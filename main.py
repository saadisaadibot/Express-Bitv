# -*- coding: utf-8 -*-
"""
Abosiyah Lite — Trend Hunter (soft scoring + dead-guard)
- /scan: يبدأ حلقة سكان جديدة (RUN_ID) ويُنهي القديمة فورًا.
- Soft scoring مُحسّن لترجيح الترند/الاختراق/الزخم + Orderbook.
- Dead-Guard: يستبعد الأزواج "الميّتة" (حركة/حجم شبه معدومين) بدون فلاتر قاتلة.
- يستبعد تلقائيًا الأزواج المستقرة (USDT/USDC/EUR/…).
- إعادة محاولة كل RETRY_SECS حتى يلتقط مرشح ويرسله لصقر.

ENV الأساسية:
  BOT_TOKEN, CHAT_ID
  SAQAR_WEBHOOK
  EXCHANGE=bitvavo, QUOTE=EUR
  TOP_UNIVERSE=150, MAX_WORKERS=6, REQUEST_SLEEP_MS=40, MAX_RPS=8, REPORT_TOP3=1
  AUTOSCAN_ON_READY=1
  RETRY_SECS=60

Tuning (ENV اختياري):
  MIN_ATR_PCT=0.12        # حد أدنى للتذبذب 1م (٪ من السعر)
  MIN_ADX=9               # حد أدنى لقوة الاتّجاه
  MIN_VSPIKE=1.25         # حد أدنى لسبايك الحجم (آخر شمعة / ميديان 20)
  MIN_RANGE_30M=0.35      # حد أدنى لنطاق 30 دقيقة (%)
  MAX_SPREAD_BP=35        # حد أقصى سبريد (basis points = 1/100 من ٪)
"""

import os, time, statistics as st, requests, ccxt
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
SAQAR_URL   = (os.getenv("SAQAR_WEBHOOK","").strip().rstrip("/"))

EXCHANGE    = os.getenv("EXCHANGE","bitvavo").lower()
QUOTE       = os.getenv("QUOTE","EUR").upper()

TOP_UNIVERSE      = int(os.getenv("TOP_UNIVERSE","150"))
MAX_WORKERS       = max(1, int(os.getenv("MAX_WORKERS","6")))
REQUEST_SLEEP_MS  = int(os.getenv("REQUEST_SLEEP_MS","40"))
MAX_RPS           = float(os.getenv("MAX_RPS","8"))
REPORT_TOP3       = int(os.getenv("REPORT_TOP3","1"))
AUTOSCAN_ON_READY = int(os.getenv("AUTOSCAN_ON_READY","1"))
RETRY_SECS        = int(os.getenv("RETRY_SECS","60"))

# Tuning
MIN_ATR_PCT   = float(os.getenv("MIN_ATR_PCT","0.12"))
MIN_ADX       = float(os.getenv("MIN_ADX","9"))
MIN_VSPIKE    = float(os.getenv("MIN_VSPIKE","1.25"))
MIN_RANGE_30M = float(os.getenv("MIN_RANGE_30M","0.35"))
MAX_SPREAD_BP = float(os.getenv("MAX_SPREAD_BP","35"))

# ===== Stable bases to exclude =====
STABLE_BASES = set((os.getenv("STABLE_BASES","USDT,USDC,EUR,DAI,TUSD,FDUSD,USDP").upper()).split(","))

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

def fetch_ohlcv(sym, tf, limit):
    try: throttle(); return _ex.fetch_ohlcv(sym, tf, limit=limit) or []
    except: return []

def fetch_orderbook(sym, depth=10):
    try:
        throttle(); ob = _ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"): return None
        bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
        spr_bp = (ask - bid)/((ask+bid)/2)*10000.0
        bv = sum(float(x[1]) for x in ob["bids"][:depth])
        av = sum(float(x[1]) for x in ob["asks"][:depth])
        imb = bv/max(av,1e-9)
        ob3b = sum(float(x[1]) for x in ob["bids"][:3]) / max(bv,1e-9)
        return {"bid":bid,"ask":ask,"spread_bp":spr_bp,"bid_imb":imb, "ob3":ob3b}
    except: return None

# ===== إرسال إلى صقر (no secret) =====
def send_saqar(base: str):
    if not SAQAR_URL:
        tg_send("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    url = SAQAR_URL + "/hook"
    payload = {"action":"buy","coin":base.upper()}
    try:
        r = requests.post(url, json=payload, timeout=(6,20))
        if 200 <= r.status_code < 300:
            tg_send(f"📡 أرسلت {base} إلى صقر | {r.status_code}")
            return True
        tg_send(f"❌ فشل إرسال {base} لصقر | status={r.status_code} | {r.text[:160]}")
    except Exception as e:
        tg_send(f"❌ خطأ إرسال لصقر: {e}")
    return False

# ===== مؤشرات خفيفة =====
def _ema_series(arr, n):
    if not arr or len(arr) < 2: return []
    k = 2.0/(n+1.0); out=[arr[0]]
    for v in arr[1:]: out.append(v*k + out[-1]*(1-k))
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
    return (abs(pDI-mDI)/max(pDI+mDI, 1e-9))*100.0

# ===== Universe =====
def list_top_by_1h_volume():
    mk = _ex.load_markets()
    syms = [s for s,i in mk.items() if i.get("active",True) and i.get("quote")==QUOTE]
    # وسّع الكون ثم خذ الأعلى بسيولة الساعة
    rows=[]
    for s in syms:
        base = s.split("/")[0].upper()
        if base in STABLE_BASES:  # استبعاد الستايبلز
            continue
        o1h = fetch_ohlcv(s, "1h", 2)
        if not o1h: continue
        close=float(o1h[-1][4]); vol=float(o1h[-1][5]); q=close*vol
        rows.append((s,q)); diplomatic_sleep(REQUEST_SLEEP_MS)
    rows.sort(key=lambda x:x[1], reverse=True)
    # خذ أعلى 50 للترشّح التفصيلي
    return [s for s,_ in rows[:50]]

# ===== Dead-Guard (استبعاد الميّت) =====
def _is_dead(closes, highs, lows, vols, ob):
    lc = closes[-1]
    atr = _atr(highs, lows, closes, 14) or 0.0
    atr_pct = (atr/lc)*100.0 if lc>0 and atr>0 else 0.0

    # نطاق 30 دقيقة
    last30 = closes[-31:] if len(closes)>=31 else closes
    r30 = ((max(last30)-min(last30))/max(lc,1e-9))*100.0 if last30 else 0.0

    # حجم
    prev20 = vols[-21:-1] if len(vols) >= 21 else vols[:-1]
    medv = st.median([v for v in prev20 if v>0]) if prev20 else 0.0
    v_spike = (vols[-1]/max(medv,1e-9)) if medv>0 else 0.0

    # ADX
    adx = _adx(highs, lows, closes, 14) or 0.0

    # سبريد
    spr = ob["spread_bp"]

    # قرار ميّت؟
    return (atr_pct < MIN_ATR_PCT and adx < MIN_ADX and v_spike < MIN_VSPIKE and r30 < MIN_RANGE_30M) or (spr > MAX_SPREAD_BP)

# ===== تقييم مرشح (Soft) =====
def _eval_fast(sym: str):
    base = sym.split("/")[0].upper()
    if base in STABLE_BASES:
        return None

    ob = fetch_orderbook(sym, depth=10)
    if not ob: return None

    o1 = fetch_ohlcv(sym, "1m", 240)
    if len(o1) < 60: return None

    closes=[float(x[4]) for x in o1]
    highs =[float(x[2]) for x in o1]
    lows  =[float(x[3]) for x in o1]
    vols  =[float(x[5]) for x in o1]
    lc = closes[-1]

    if _is_dead(closes, highs, lows, vols, ob):
        return None  # طنّش الميّت

    ema50  = _ema_series(closes, 50)
    ema200 = _ema_series(closes, 200)
    ema50  = ema50[-1]  if ema50  else None
    ema200 = ema200[-1] if ema200 else None
    trend_up = (ema50 is not None and ema200 is not None and ema50 > ema200)

    rsi = _rsi(closes, 14) or 50.0
    adx = _adx(highs, lows, closes, 14) or 0.0
    atr = _atr(highs, lows, closes, 14) or 0.0
    atr_pct = (atr/lc)*100.0 if lc>0 and atr>0 else 0.0

    # اختراق أعلى 20 شمعة سابقة
    prev20 = o1[-21:-1] if len(o1) >= 21 else o1[:-1]
    h20 = max(float(x[2]) for x in prev20) if prev20 else lc
    brk_pct = max(((lc/max(h20,1e-9))-1.0)*100.0, 0.0)

    # زخم لحظي ومتوسط
    def roc(n):
        if len(closes) <= n: return 0.0
        try: return (closes[-1]/closes[-n]-1.0)*100.0
        except: return 0.0
    r1  = roc(2)   # ~1-2 دقيقة
    r3  = roc(4)   # ~3-4 د
    r5  = roc(6)   # ~5-6 د
    r30 = roc(30)  # ~30 د
    r90 = roc(90)  # ~90 د

    # سبايك حجم مقابل ميديان 20
    medv = st.median([float(x[5]) for x in prev20 if float(x[5])>0]) if prev20 else 0.0
    v_spike = (float(o1[-1][5])/max(medv,1e-9)) if medv>0 else 0.0

    # ——— Soft scoring (متحيّز للترند/الاختراق) ———
    score = 0.0
    # اتجاه عام + زخم أفق متعدد
    score += 2.0 * (1.0 if trend_up else -0.8)
    score += 0.80 * max(r1,0.0) + 0.60 * max(r3,0.0) + 0.40 * max(r5,0.0) + 0.50 * max(r30,0.0) + 0.35 * max(r90,0.0)
    # اختراق + سبايك فوليوم
    score += 1.40 * brk_pct
    score += 0.90 * min(v_spike, 8.0)
    # ADX sweet-spot ~ 18–35
    score += 0.70 * min(adx/20.0, 2.0)
    # ATR% وسط (حركة كافية دون إفراط)
    if atr_pct > 0:
        score += 0.60 * (1.0 - min(abs(atr_pct-0.30)/0.30, 1.0))
    # Orderbook: imbalance + dominance top3
    score += 0.70 * min(ob["bid_imb"], 3.0)
    score += 0.45 * ob.get("ob3", 0.0)
    # Spread عقوبة
    score -= 0.25 * (ob["spread_bp"]/10.0)

    return {"symbol": sym, "base": base, "score": float(score),
            "rsi": float(rsi), "adx": float(adx), "atr_pct": float(atr_pct),
            "brk": float(brk_pct), "r1": float(r1), "r3": float(r3),
            "r30": float(r30), "r90": float(r90),
            "spr": float(ob["spread_bp"]), "imb": float(ob["bid_imb"]),
            "dom3": float(ob.get("ob3",0.0)), "vsp": float(v_spike)}

def run_filter_and_pick():
    top_syms = list_top_by_1h_volume()
    if not top_syms: return None, []
    cands=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(_eval_fast, s): s for s in top_syms}
        for f in as_completed(futs):
            r = f.result()
            if r: cands.append(r)
            diplomatic_sleep(REQUEST_SLEEP_MS)
    if not cands: return None, []
    cands.sort(key=lambda r: r["score"], reverse=True)
    return cands[0], cands[:3]

# ===== تشغيل بلا قفل عبر RUN_ID =====
RUN_ID = 0

def _top3_lines(top3):
    lines=[]
    for i,r in enumerate(top3,1):
        lines.append(
            f"{i}) {r['symbol']} | sc={r['score']:.2f} | brk={r['brk']:.2f}% "
            f"r1={r['r1']:.2f}% r3={r['r3']:.2f}% r30={r['r30']:.2f}% "
            f"ADX={r['adx']:.1f} ATR%={r['atr_pct']:.3f}% Vx={r['vsp']:.2f} "
            f"spr={r['spr']:.0f}bp ob3={r['dom3']:.2f}"
        )
    return lines

def _pick_once():
    top1, top3 = run_filter_and_pick()
    if REPORT_TOP3 and top3:
        tg_send("🎯 Top3:\n" + "\n".join(_top3_lines(top3)))
    return top1

def _scan_loop(my_id: int):
    tg_send(f"🔎 بدء فلترة ترند (run={my_id})…")
    while True:
        if my_id != RUN_ID:
            tg_send(f"↩️ وقفت حلقة أقدم (run={my_id}) لوجود أحدث (run={RUN_ID}).")
            return
        top1 = _pick_once()
        if top1:
            tg_send(f"🧠 Top1: {top1['symbol']} | score={top1['score']:.2f}")
            ok = send_saqar(top1["base"])
            tg_send(f"📡 أرسلت {top1['base']} إلى صقر | ok={ok} | run={my_id}")
            return
        tg_send(f"⏸ لا يوجد مرشح (قد تكون السوق هادئة) — إعادة بعد {RETRY_SECS}s (run={my_id}).")
        for _ in range(RETRY_SECS):
            if my_id != RUN_ID: return
            time.sleep(1)

def start_scan_loop():
    global RUN_ID
    RUN_ID += 1
    this_id = RUN_ID
    Thread(target=_scan_loop, args=(this_id,), daemon=True).start()
    tg_send(f"▶️ scan triggered (run={this_id})")

# ===== HTTP =====
@app.route("/scan", methods=["GET"])
def scan_manual_http():
    start_scan_loop()
    return jsonify(ok=True, run=RUN_ID), 200

@app.route("/ready", methods=["POST"])
def on_ready():
    data = request.get_json(force=True) or {}
    coin   = (data.get("coin") or "").upper()
    reason = data.get("reason") or "-"
    pnl    = data.get("pnl_eur")
    try: pnl_txt = f"{float(pnl):.4f}€" if pnl is not None else "—"
    except: pnl_txt = "—"
    tg_send(f"✅ صقر أنهى {coin} (سبب={reason}, ربح={pnl_txt}).")
    if AUTOSCAN_ON_READY == 1:
        start_scan_loop()
    return jsonify(ok=True, run=RUN_ID)

# ===== Telegram webhook =====
@app.route("/webhook", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str(msg.get("chat", {}).get("id", "")) or None
    text = (msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id): return jsonify(ok=True), 200
    if text.startswith("/scan"):
        start_scan_loop(); return jsonify(ok=True), 200
    if text.startswith("/ping"):
        tg_send(f"pong ✅ | run={RUN_ID}"); return jsonify(ok=True), 200
    tg_send("أوامر: /scan ، /ping"); return jsonify(ok=True), 200

@app.get("/")
def home(): return f"Abosiyah Lite — Trend Hunter ✅ (run={RUN_ID})", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))