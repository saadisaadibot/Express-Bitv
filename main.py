# -*- coding: utf-8 -*-
"""
Abosiyah Lite — Smart Top1 on demand (+ auto-rescan loop)
- اختيار مرشح ذكي: EMA(1m&5m) trend + Anti-Extension + RSI + ADX + ATR% + Breakout + Pulse + OB + Spread
- لا يوجد LINK_SECRET (لا أسرار بين السيرفرين)
- /scan: يبدأ حلقة سكان حتى يرسل عملة بنجاح إلى صقر
- /ready: يطلق سكان تلقائي (إذا AUTOSCAN_ON_READY=1)

ENV:
  BOT_TOKEN, CHAT_ID
  SAQAR_WEBHOOK              # مثال: https://saqer.up.railway.app  (بدون سلاش أخير)
  EXCHANGE=bitvavo, QUOTE=EUR
  TOP_UNIVERSE=120, MAX_WORKERS=6, REQUEST_SLEEP_MS=40, MAX_RPS=8, REPORT_TOP3=1
  AUTOSCAN_ON_READY=0
  RESCAN_BASE_SEC=60         # فترة إعادة المحاولة عند عدم وجود مرشح
  RESCAN_JITTER_SEC=30       # عشوائية بسيطة لتجنب الاصطدام
  LOOP_MAX_MIN=30            # حد أقصى لمدة الحلقة الواحدة (دقائق) حفاظًا على الموارد
"""

import os, time, math, random, statistics as st, requests, ccxt
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
SAQAR_URL   = os.getenv("SAQAR_WEBHOOK","").strip().rstrip("/")

EXCHANGE    = os.getenv("EXCHANGE","bitvavo").lower()
QUOTE       = os.getenv("QUOTE","EUR").upper()

TOP_UNIVERSE      = int(os.getenv("TOP_UNIVERSE","120"))
MAX_WORKERS       = max(1, int(os.getenv("MAX_WORKERS","6")))
REQUEST_SLEEP_MS  = int(os.getenv("REQUEST_SLEEP_MS","40"))
MAX_RPS           = float(os.getenv("MAX_RPS","8"))
REPORT_TOP3       = int(os.getenv("REPORT_TOP3","1"))
AUTOSCAN_ON_READY = int(os.getenv("AUTOSCAN_ON_READY","0"))

RESCAN_BASE_SEC   = int(os.getenv("RESCAN_BASE_SEC","60"))
RESCAN_JITTER_SEC = int(os.getenv("RESCAN_JITTER_SEC","30"))
LOOP_MAX_MIN      = int(os.getenv("LOOP_MAX_MIN","30"))

# ===== Telegram =====
def tg_send(text):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID or None, "text": text}, timeout=8)
    except Exception as e:
        print("tg err:", e)

def _auth_chat(cid: str) -> bool:
    return (not CHAT_ID) or (str(cid) == str(CHAT_ID))

# ===== Exchange + throttle =====
_ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})
_th_lock, _last_ts, _min_dt = Lock(), 0.0, 1.0/max(0.1, MAX_RPS)
def throttle():
    global _last_ts
    with _th_lock:
        now = time.time(); wait = _min_dt - (now - _last_ts)
        if wait > 0: time.sleep(wait)
        _last_ts = time.time()

def diplomatic_sleep(ms):
    if ms>0: time.sleep(ms/1000.0)

def fetch_ohlcv_safe(sym, tf, limit):
    try:
        throttle()
        return _ex.fetch_ohlcv(sym, tf, limit=limit) or []
    except:
        return []

def fetch_orderbook(sym, depth=10):
    try:
        throttle(); ob = _ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"): return None
        bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
        spr_bp = (ask - bid)/((ask+bid)/2)*10000.0
        bv = sum(float(x[1]) for x in ob["bids"][:depth])
        av = sum(float(x[1]) for x in ob["asks"][:depth])
        imb = bv/max(av,1e-9)
        return {"bid":bid,"ask":ask,"spread_bp":spr_bp,"bid_imb":imb}
    except:
        return None

# ===== إرسال إلى صقر (بدون أي سر) =====
def send_saqar(base: str) -> bool:
    if not SAQAR_URL:
        tg_send("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    url = SAQAR_URL + "/hook"
    payload = {"action":"buy","coin":base.upper()}  # صقر يحتاج هدول فقط
    headers = {"Content-Type":"application/json"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=(6,20))
        if 200 <= r.status_code < 300:
            tg_send(f"📡 أرسلت {base} إلى صقر | {r.status_code}")
            return True
        tg_send(f"❌ فشل إرسال {base} لصقر | status={r.status_code} | {r.text[:160]}")
    except Exception as e:
        tg_send(f"❌ خطأ إرسال لصقر: {e}")
    return False

# ===== مؤشرات خفيفة =====
def _series_ema(arr, n):
    if not arr or len(arr) < n: return []
    k = 2.0/(n+1.0)
    out = [arr[0]]
    for v in arr[1:]:
        out.append(v*k + out[-1]*(1-k))
    return out

def _rsi(closes, n=14):
    if len(closes) < n+1: return None
    gains=0.0; losses=0.0
    for i in range(-n,0):
        d = closes[i]-closes[i-1]
        if d>=0: gains += d
        else: losses += -d
    avg_gain = gains/n; avg_loss = (losses/n) if losses>0 else 1e-9
    rs = avg_gain/avg_loss
    return 100.0 - (100.0/(1.0+rs))

def _atr(highs, lows, closes, n=14):
    if len(closes) < n+1: return None
    trs=[]
    for i in range(1,len(closes)):
        tr=max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:n])/n
    for tr in trs[n:]:
        atr = (atr*(n-1)+tr)/n
    return atr

def _adx(highs, lows, closes, n=14):
    if len(closes) < n+1: return None
    plus_dm=[]; minus_dm=[]; trs=[]
    for i in range(1,len(closes)):
        up = highs[i]-highs[i-1]
        dn = lows[i-1]-lows[i]
        plus_dm.append(up if (up>dn and up>0) else 0.0)
        minus_dm.append(dn if (dn>up and dn>0) else 0.0)
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    tr14=sum(trs[:n]); pDM14=sum(plus_dm[:n]); mDM14=sum(minus_dm[:n])
    for i in range(n,len(trs)):
        tr14  = tr14  - (tr14/n)  + trs[i]
        pDM14 = pDM14 - (pDM14/n) + plus_dm[i]
        mDM14 = mDM14 - (mDM14/n) + minus_dm[i]
    if tr14<=0: return 0.0
    pDI=(pDM14/tr14)*100.0; mDI=(mDM14/tr14)*100.0
    dx=(abs(pDI-mDI)/max(pDI+mDI,1e-9))*100.0
    return dx  # تقريب

# ===== Universe: أعلى سيولة بالساعة ثم تصفية دقيقة =====
def list_top_by_1h_volume():
    mk = _ex.load_markets()
    syms = [s for s,i in mk.items() if i.get("active",True) and i.get("quote")==QUOTE]
    syms = syms[:max(10,min(TOP_UNIVERSE,len(syms)))]
    rows=[]
    for s in syms:
        o1h = fetch_ohlcv_safe(s, "1h", 2)
        if not o1h: continue
        close=float(o1h[-1][4]); vol=float(o1h[-1][5]); q=close*vol
        rows.append((s,q)); diplomatic_sleep(REQUEST_SLEEP_MS)
    rows.sort(key=lambda x:x[1], reverse=True)
    return [s for s,_ in rows[:50]]

# ===== تقييم ذكي (Multi-TF + Anti-Extension + Pulse) =====
def _eval_fast(sym: str):
    ob = fetch_orderbook(sym, depth=10)
    if not ob: 
        return None

    o1 = fetch_ohlcv_safe(sym, "1m", 240)
    if len(o1) < 210:  # بدنا EMA200
        return None

    o5 = fetch_ohlcv_safe(sym, "5m", 200)  # اتجاه أبطأ
    if len(o5) < 120:
        return None

    closes1=[float(x[4]) for x in o1]
    highs1 =[float(x[2]) for x in o1]
    lows1  =[float(x[3]) for x in o1]

    lc = closes1[-1]

    # اتجاه 1m
    ema50_1  = _series_ema(closes1, 50)[-1]
    ema200_1 = _series_ema(closes1, 200)[-1]
    trend1 = (ema50_1 is not None and ema200_1 is not None and ema50_1 > ema200_1)

    # اتجاه 5m
    closes5=[float(x[4]) for x in o5]
    ema50_5  = _series_ema(closes5, 50)[-1]
    ema200_5 = _series_ema(closes5, 200)[-1]
    trend5 = (ema50_5 is not None and ema200_5 is not None and ema50_5 > ema200_5)

    trend_up = trend1 and trend5  # تأكيد متعدد الأطر

    rsi = _rsi(closes1, 14) or 50.0
    adx = _adx(highs1, lows1, closes1, 14) or 0.0
    atr = _atr(highs1, lows1, closes1, 14) or 0.0
    atr_pct = (atr/lc)*100.0 if lc>0 and atr>0 else 0.0

    # Breakout بسيط مقابل أعلى 20
    prev20 = o1[-21:-1]
    h20 = max(float(x[2]) for x in prev20) if prev20 else lc
    brk_pct = max(((lc/max(h20,1e-9))-1.0)*100.0, 0.0)

    # لحظي
    def pct(a,b): 
        try: return (a/b-1.0)*100.0
        except: return 0.0
    mom1 = pct(closes1[-1], closes1[-2])
    mom3 = pct(closes1[-1], closes1[-4]) if len(closes1)>=4 else 0.0
    mom5 = pct(closes1[-1], closes1[-6]) if len(closes1)>=6 else 0.0

    # Anti-extension: لا نشتري إذا السعر فوق EMA20 بأكثر من ~1×ATR
    ema20_1 = _series_ema(closes1, 20)[-1]
    ext_pct = ((lc/ema20_1)-1.0)*100.0 if ema20_1 else 0.0
    over_extended = (atr > 0 and ext_pct > (atr_pct))  # تقريب: فوق 1×ATR%

    # شروط صلبة
    if not trend_up: 
        return None
    if adx < 20: 
        return None
    if not (52.0 <= rsi <= 72.0):  # نطاق أنظف للاندفاعة الصحية
        return None
    if ob["spread_bp"] > 20.0:
        return None
    if ob["bid_imb"] < 1.10:
        return None
    if not (0.08 <= atr_pct <= 0.80):
        return None
    if over_extended:
        return None

    # ميل EMA8 كقياس سرعة
    ema8_1_series = _series_ema(closes1, 8)
    slope_pct = 0.0
    if len(ema8_1_series) >= 3:
        e_now = ema8_1_series[-1]; e_prev = ema8_1_series[-3]
        if e_prev > 0:
            slope_pct = (e_now/e_prev - 1.0) * 100.0

    # Pulse تقديري
    pulse = max(mom1,0.0) + 0.5*max(mom3,0.0) + 0.25*max(mom5,0.0) + 0.5*brk_pct + 0.5*(atr_pct)

    # قرب RSI من 60 أفضل
    rsi_bias = -abs(rsi-60.0)/6.0

    # Score
    score = (
        1.15*brk_pct +
        0.95*min(pulse, 6.0) +
        0.90*(min((abs(mom1)+abs(mom3))/2.0, 2.0)) +
        0.70*(slope_pct/0.30) +   # كل ~0.30%/دقيقتين يرفع التقييم
        0.60*(adx/30.0) +
        0.50*min(ob["bid_imb"], 2.0) +
        0.40*rsi_bias -
        0.25*(ob["spread_bp"]/10.0)
    )

    return {
        "symbol": sym,
        "base": sym.split("/")[0],
        "score": float(score),
        "lc": lc,
        "trend_multiTF": trend_up,
        "rsi": float(rsi),
        "adx": float(adx),
        "atr_pct": float(atr_pct),
        "brk": float(brk_pct),
        "mom1": float(mom1),
        "mom3": float(mom3),
        "mom5": float(mom5),
        "slope8": float(slope_pct),
        "spr": float(ob["spread_bp"]),
        "imb": float(ob["bid_imb"]),
        "ext_pct": float(ext_pct)
    }

def run_filter_and_pick():
    top_syms = list_top_by_1h_volume()
    if not top_syms: 
        return None, []

    def _pick_once(relax=False):
        cands=[]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {pool.submit(_eval_fast, s): s for s in top_syms}
            for f in as_completed(futs):
                r = f.result()
                if r:
                    if relax:
                        r["score"] += 0.15  # دفشة بسيطة في الوضع المرن
                    cands.append(r)
                diplomatic_sleep(REQUEST_SLEEP_MS)
        return cands

    cands = _pick_once(relax=False)
    if not cands:
        cands = _pick_once(relax=True)

    if not cands:
        return None, []

    cands.sort(key=lambda r: r["score"], reverse=True)
    return cands[0], cands[:3]

# ===== حلقة سكان تلقائية حتى الإرسال =====
SCANNING = False

def _sleep_with_jitter():
    sec = RESCAN_BASE_SEC + random.randint(0, max(0, RESCAN_JITTER_SEC))
    time.sleep(max(5, sec))

def scan_loop_until_sent():
    global SCANNING
    if SCANNING:
        tg_send("⏳ سكان شغّال الآن… تجاهلت الطلب المكرر."); 
        return
    SCANNING = True
    t_end = time.time() + LOOP_MAX_MIN*60
    try:
        tg_send("🔎 بدء فلترة (1h سيولة → 1m مؤشرات+OB)…")
        while time.time() < t_end:
            top1, top3 = run_filter_and_pick()
            if top1:
                if REPORT_TOP3 and top3:
                    lines=[]
                    for i,r in enumerate(top3,1):
                        lines.append(
                            f"{i}) {r['symbol']} | sc={r['score']:.2f}  "
                            f"RSI={r['rsi']:.1f} ADX={r['adx']:.1f} ATR%={r['atr_pct']:.3f}% "
                            f"brk={r['brk']:.2f}% mom1={r['mom1']:.2f}% slope8={r['slope8']:.2f}% "
                            f"spr={r['spr']:.0f}bp ob={r['imb']:.2f} ext={r['ext_pct']:.2f}%"
                        )
                    tg_send("🎯 Top3:\n" + "\n".join(lines))
                tg_send(
                    f"🧠 Top1: {top1['symbol']} | score={top1['score']:.2f}\n"
                    f"↗️ اتجاه: Multi-TF={top1['trend_multiTF']} | brk={top1['brk']:.2f}%\n"
                    f"💓 نبض: mom1={top1['mom1']:.2f}% slope8={top1['slope8']:.2f}% ATR%={top1['atr_pct']:.3f}%\n"
                    f"🧪 RSI={top1['rsi']:.1f} ADX={top1['adx']:.1f} | OB ob={top1['imb']:.2f}, spr={top1['spr']:.0f}bp\n"
                    f"🧊 امتداد عن EMA20: {top1['ext_pct']:.2f}%"
                )
                ok = send_saqar(top1["base"])
                tg_send(f"📡 أرسلت {top1['base']} إلى صقر | ok={ok}")
                if ok:
                    return  # انتهت الحلقة بنجاح
            else:
                tg_send("⏸ لا يوجد مرشح مناسب حالياً. أعيد المحاولة بعد قليل…")
            _sleep_with_jitter()
        tg_send("⏹ انتهت مدة الحلقة دون إرسال — يمكنك /scan لإعادة التشغيل.")
    finally:
        SCANNING = False

# ===== HTTP =====
@app.route("/scan", methods=["GET"])
def scan_manual_http():
    import threading; threading.Thread(target=scan_loop_until_sent, daemon=True).start()
    return jsonify(ok=True, msg="scan loop started"), 200

@app.route("/ready", methods=["POST"])
def on_ready():
    # صقر سيرسل: {"coin":"ADA","reason":"tp_filled|sl_triggered|buy_failed","pnl_eur":null}
    data = request.get_json(force=True) or {}
    coin   = (data.get("coin") or "").upper()
    reason = data.get("reason") or "-"
    pnl    = data.get("pnl_eur")
    try: pnl_txt = f"{float(pnl):.4f}€" if pnl is not None else "—"
    except: pnl_txt = "—"
    tg_send(f"✅ صقر أنهى {coin} (سبب={reason}, ربح={pnl_txt}).")
    if AUTOSCAN_ON_READY == 1:
        import threading; threading.Thread(target=scan_loop_until_sent, daemon=True).start()
    return jsonify(ok=True)

# ===== Telegram webhook =====
@app.route("/webhook", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str(msg.get("chat", {}).get("id", "")) or None
    text = (msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id): return jsonify(ok=True), 200
    if text.startswith("/scan"):
        import threading; threading.Thread(target=scan_loop_until_sent, daemon=True).start()
        return jsonify(ok=True), 200
    if text.startswith("/ping"):
        tg_send("pong ✅"); return jsonify(ok=True), 200
    tg_send("أوامر: /scan ، /ping"); return jsonify(ok=True), 200

@app.get("/")
def home(): return "Abosiyah Lite — Smart Top1 + auto-rescan ✅", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))