# -*- coding: utf-8 -*-
"""
Abosiyah Lite — No-hard-filters Top1 Scanner (soft penalties)
- بلا قفل: كل /scan يزيد RUN_ID والحلقة الأقدم تنسحب بهدوء.
- يعيد المحاولة كل RETRY_SECS حتى يلقى مرشح ويرسله لصقر.
- /ready يعيد تشغيل سكان تلقائياً لو AUTOSCAN_ON_READY=1.
- اختيار المرشح يتم بنقاط مع عقوبات ناعمة بدل الاستبعاد.
"""

import os, time, statistics as st, math, requests, ccxt
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)

# Telegram / Saqer
BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
SAQAR_URL   = os.getenv("SAQAR_WEBHOOK","").strip().rstrip("/")

# Exchange
EXCHANGE    = os.getenv("EXCHANGE","bitvavo").lower()
QUOTE       = os.getenv("QUOTE","EUR").upper()

# Scanner tuning
TOP_UNIVERSE      = int(os.getenv("TOP_UNIVERSE","120"))
MAX_WORKERS       = max(1, int(os.getenv("MAX_WORKERS","6")))
REQUEST_SLEEP_MS  = int(os.getenv("REQUEST_SLEEP_MS","40"))
MAX_RPS           = float(os.getenv("MAX_RPS","8"))
REPORT_TOP3       = int(os.getenv("REPORT_TOP3","1"))
AUTOSCAN_ON_READY = int(os.getenv("AUTOSCAN_ON_READY","1"))
RETRY_SECS        = int(os.getenv("RETRY_SECS","60"))

# Soft-mode knobs
STRICTNESS            = float(os.getenv("STRICTNESS", "1.0"))   # 0..1 (1 = مرن جداً)
MAX_SPREAD_BP_SOFT    = float(os.getenv("MAX_SPREAD_BP_SOFT", "35"))
MIN_CANDLES_1M        = int(os.getenv("MIN_CANDLES_1M", "60"))

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
        return {"bid":bid,"ask":ask,"spread_bp":spr_bp,"bid_imb":imb}
    except: return None

def fetch_trades(sym, limit=300):
    try: throttle(); return _ex.fetch_trades(sym, limit=limit) or []
    except: return []

# ===== إرسال إلى صقر (no secret) =====
def send_saqar(base: str):
    if not SAQAR_URL:
        tg_send("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    url = SAQAR_URL + "/hook"
    payload = {"action":"buy","coin":base.upper()}
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
    return dx

# ===== Universe + تقييم مرن =====
def list_top_by_1h_volume():
    mk = _ex.load_markets()
    syms = [s for s,i in mk.items() if i.get("active",True) and i.get("quote")==QUOTE]
    syms = syms[:max(10,min(TOP_UNIVERSE,len(syms)))]
    rows=[]
    for s in syms:
        o1h = fetch_ohlcv(s, "1h", 2)
        if not o1h: continue
        close=float(o1h[-1][4]); vol=float(o1h[-1][5]); q=close*vol
        rows.append((s,q)); diplomatic_sleep(REQUEST_SLEEP_MS)
    rows.sort(key=lambda x:x[1], reverse=True)
    return [s for s,_ in rows[:50]]

def _eval_fast(sym: str):
    ob = fetch_orderbook(sym, depth=15)
    if not ob: return None

    o1 = fetch_ohlcv(sym, "1m", 240)
    if len(o1) < MIN_CANDLES_1M:
        return None

    closes=[float(x[4]) for x in o1]
    highs =[float(x[2]) for x in o1]
    lows  =[float(x[3]) for x in o1]
    lc = closes[-1]

    # مؤشرات أساسية
    def pct(a,b):
        try: return (a/b-1.0)*100.0
        except: return 0.0

    ema50s  = _series_ema(closes, 50);  ema50  = ema50s[-1]  if ema50s  else None
    ema200s = _series_ema(closes, 200); ema200 = ema200s[-1] if ema200s else None
    trend_up = (ema50 is not None and ema200 is not None and ema50 > ema200)

    rsi = _rsi(closes, 14) or 50.0
    adx = _adx(highs, lows, closes, 14) or 0.0
    atr = _atr(highs, lows, closes, 14) or 0.0
    atr_pct = (atr/lc)*100.0 if lc>0 and atr>0 else 0.0

    prev20 = o1[-21:-1] if len(o1) >= 21 else o1[:-1]
    h20 = max(float(x[2]) for x in prev20) if prev20 else lc
    brk_pct = max(((lc/max(h20,1e-9))-1.0)*100.0, 0.0) if lc>0 else 0.0

    medv = st.median([float(x[5]) for x in prev20 if float(x[5])>0]) if prev20 else 0.0
    v_spike = (float(o1[-1][5])/max(medv,1e-9)) if medv>0 else 0.0

    mom1 = pct(closes[-1], closes[-2]) if len(closes)>=2 else 0.0
    mom3 = pct(closes[-1], closes[-4]) if len(closes)>=4 else 0.0
    mom5 = pct(closes[-1], closes[-6]) if len(closes)>=6 else 0.0

    # Tape speed & dominance (آخر 60ث)
    recent = fetch_trades(sym, limit=300)
    now_ms = int(time.time()*1000); min_ms = now_ms - 60_000
    t_cnt = 0; buy_vol=0.0; sell_vol=0.0
    for t in recent:
        ts = int(t.get('timestamp') or 0)
        if ts < min_ms: continue
        amt = float(t.get('amount') or 0.0)
        sd  = (t.get('side') or '').lower()
        t_cnt += 1
        if sd == 'buy':  buy_vol  += amt
        elif sd == 'sell': sell_vol += amt
    tps    = t_cnt/60.0                          # trades per second
    buydom = (buy_vol / max(buy_vol+sell_vol, 1e-9)) if (buy_vol+sell_vol)>0 else 0.0

    # Book pressure (3 مستويات)
    try:
        bsum = sum(float(x[1]) for x in _ex.fetch_order_book(sym, limit=3)['bids'][:3]); throttle()
        asum = sum(float(x[1]) for x in _ex.fetch_order_book(sym, limit=3)['asks'][:3])
        ob3  = bsum/max(asum,1e-9)
    except:
        ob3 = ob["bid_imb"]

    # ======= Soft penalties بدل الفلاتر =======
    pen = 0.0; s = STRICTNESS
    pen += s * max(0.0, (ob["spread_bp"] - MAX_SPREAD_BP_SOFT)/10.0)
    if trend_up is False:     pen += 1.5 * s
    pen += s * max(0.0, (48.0 - rsi)/12.0)      # RSI منخفض جداً
    pen += s * max(0.0, (rsi - 78.0)/12.0)      # RSI عالي جداً
    pen += s * max(0.0, (22.0 - adx)/6.0)       # ADX ضعيف
    pen += s * max(0.0, (0.08 - atr_pct)/0.04)  # ATR ضعيف جداً
    pen += s * max(0.0, (atr_pct - 0.90)/0.30)  # تذبذب مبالغ
    pen += s * max(0.0, (1.20 - ob3) * 1.2)     # ضغط شراء ضعيف
    pen += s * max(0.0, (0.55 - buydom) * 2.0)  # هيمنة المشترين ضعيفة
    pen += s * max(0.0, (2.0  - tps)    * 0.8)  # التيب بطيء

    # Score إيجابي
    rsi_bias = -abs(rsi-60.0)/6.0
    pulse = max(mom1,0.0) + 0.5*max(mom3,0.0) + 0.25*max(mom5,0.0) + 0.5*brk_pct + 0.5*(atr_pct/1.0)
    score = (
        1.50*min(tps, 6.0) +
        1.10*(buydom*10.0) +
        1.00*min(ob3, 2.5) +
        0.90*min(v_spike, 6.0) +
        0.75*max(pulse, 0.0) +
        0.60*(adx/30.0) +
        0.40*rsi_bias -
        0.15*(ob["spread_bp"]/10.0)
    ) - pen

    return {
        "symbol": sym, "base": sym.split("/")[0], "score": float(score),
        "rsi": float(rsi), "adx": float(adx), "atr_pct": float(atr_pct),
        "brk": float(brk_pct), "mom1": float(mom1),
        "spr": float(ob["spread_bp"]), "imb": float(ob3),
        "tps": float(tps), "dom": float(buydom)
    }

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

    # تطبيع Z-score داخلي لتحسين المقارنة
    m = sum(r["score"] for r in cands)/len(cands)
    v = sum((r["score"]-m)**2 for r in cands)/max(1,len(cands)-1)
    sd = math.sqrt(max(v, 1e-9))
    for r in cands: r["score"] = (r["score"]-m)/sd

    cands.sort(key=lambda r: r["score"], reverse=True)
    return cands[0], cands[:3]

# ===== تشغيل بلا قفل عبر RUN_ID =====
RUN_ID = 0  # كل /scan يزيده وتنسحب الحلقات الأقدم

def _pick_once_with_report():
    top1, top3 = run_filter_and_pick()
    if REPORT_TOP3 and top3:
        lines = [f"{i}) {r['symbol']} | sc={r['score']:.2f} | brk={r['brk']:.2f}% "
                 f"RSI={r['rsi']:.1f} ADX={r['adx']:.1f} ATR%={r['atr_pct']:.3f}% "
                 f"tps={r['tps']:.2f}/s dom={r['dom']:.2f} spr={r['spr']:.0f}bp ob3={r['imb']:.2f}"
                 for i,r in enumerate(top3,1)]
        tg_send("🎯 Top3:\n" + "\n".join(lines))
    return top1

def _scan_loop(my_id: int):
    tg_send(f"🔎 بدء فلترة (run={my_id})…")
    while True:
        if my_id != RUN_ID: 
            tg_send(f"↩️ أوقفت حلقة قديمة (run={my_id}) بسبب سكان أحدث (run={RUN_ID}).")
            return
        top1 = _pick_once_with_report()
        if top1:
            tg_send(f"🧠 Top1: {top1['symbol']} | score={top1['score']:.2f}")
            ok = send_saqar(top1["base"])
            tg_send(f"📡 أرسلت {top1['base']} إلى صقر | ok={ok} | run={my_id}")
            return
        tg_send(f"⏸ لا يوجد مرشح — إعادة المحاولة بعد {RETRY_SECS}s (run={my_id}).")
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
def home(): return f"Abosiyah Lite — soft-penalty Top1 ✅ (run={RUN_ID})", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))