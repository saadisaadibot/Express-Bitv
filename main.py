# -*- coding: utf-8 -*-
"""
Abosiyah — Manual /scan -> send Top1 to Saqer, then wait /ready only
- Telegram notifications
- ccxt scanning with orderflow (orderbook imbalance)
- Does NOT auto-rescan on /ready (just clears busy flag)

ENV:
  BOT_TOKEN, CHAT_ID
  SAQAR_WEBHOOK              # مثال: https://saqer.up.railway.app  (بدون سلاش أخير)
  LINK_SECRET                # اختياري: للهيدر X-Link-Secret
  EXCHANGE=bitvavo, QUOTE=EUR
  TOP_UNIVERSE=120, MAX_WORKERS=6, REQUEST_SLEEP_MS=40, MAX_RPS=8, REPORT_TOP3=1
  TP_EUR_DEFAULT=0.05, SL_PCT_DEFAULT=-2
  OF_DEPTH=10, OF_WAIT_MS=1200, OF_WEIGHT=1.1, IMB_MIN=1.10
"""

import os, time, statistics as st, requests, ccxt
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
LINK_SECRET = os.getenv("LINK_SECRET","").strip()

EXCHANGE    = os.getenv("EXCHANGE","bitvavo").lower()
QUOTE       = os.getenv("QUOTE","EUR").upper()

TOP_UNIVERSE      = int(os.getenv("TOP_UNIVERSE","120"))
MAX_WORKERS       = max(1, int(os.getenv("MAX_WORKERS","6")))
REQUEST_SLEEP_MS  = int(os.getenv("REQUEST_SLEEP_MS","40"))
MAX_RPS           = float(os.getenv("MAX_RPS","8"))
REPORT_TOP3       = int(os.getenv("REPORT_TOP3","1"))

TP_EUR_DEFAULT    = float(os.getenv("TP_EUR_DEFAULT","0.05"))
SL_PCT_DEFAULT    = float(os.getenv("SL_PCT_DEFAULT","-2"))

# Orderflow tuning
OF_DEPTH   = int(os.getenv("OF_DEPTH", "10"))
OF_WAIT_MS = int(os.getenv("OF_WAIT_MS", "1200"))
OF_WEIGHT  = float(os.getenv("OF_WEIGHT", "1.1"))
IMB_MIN    = float(os.getenv("IMB_MIN", "1.10"))

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

def fetch_orderbook(sym, depth=5, with_levels=False):
    """
    يرجّع: bid, ask, spread_bp, bid_imb (bv/av), bid_vol, ask_vol
    وإذا with_levels=True يرجّع أيضاً القوائم.
    """
    try:
        throttle()
        ob = _ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"): return None
        bids = [(float(p), float(a)) for p, a in ob["bids"][:depth] if a and p]
        asks = [(float(p), float(a)) for p, a in ob["asks"][:depth] if a and p]
        if not bids or not asks: return None
        bid = bids[0][0]; ask = asks[0][0]
        mid = (bid+ask)/2.0
        spr_bp = (ask - bid)/max(mid,1e-12)*10000.0
        bv = sum(a for _,a in bids); av = sum(a for _,a in asks)
        data = {"bid":bid,"ask":ask,"spread_bp":spr_bp,
                "bid_vol":bv,"ask_vol":av,"bid_imb":(bv/max(av,1e-9))}
        if with_levels: data["bids"]=bids; data["asks"]=asks
        return data
    except: return None

def _clamp(x, lo, hi): 
    return hi if x>hi else lo if x<lo else x

def _microprice_bias_bp(bid, ask, bv, av):
    """Microprice=(ask*bv+bid*av)/(bv+av) → الانحياز عن الوسط بالـ bp."""
    mid = (bid + ask) / 2.0
    mp  = ((ask* bv) + (bid* av)) / max(bv+av,1e-12)
    return (mp - mid)/max(mid,1e-12)*10000.0

def sample_orderflow(sym, depth=None, wait_ms=None):
    """يلتقط لقطتين للدفتر ويقيس الاختلال اللحظي + تغيّره + انحياز الميكرو."""
    d = depth or OF_DEPTH
    w = wait_ms if wait_ms is not None else OF_WAIT_MS
    ob1 = fetch_orderbook(sym, depth=d, with_levels=True)
    if not ob1: return None
    time.sleep(max(0,w)/1000.0)
    ob2 = fetch_orderbook(sym, depth=d, with_levels=True)
    if not ob2: return None
    imb1 = ob1["bid_vol"]/max(ob1["ask_vol"],1e-9)
    imb2 = ob2["bid_vol"]/max(ob2["ask_vol"],1e-9)
    imb_delta = imb2 - imb1
    micro_bias = _microprice_bias_bp(ob2["bid"], ob2["ask"], ob2["bid_vol"], ob2["ask_vol"])
    bonus = 0.7*_clamp(imb2-1.0,0.0,2.0) + 0.5*_clamp(imb_delta,0.0,1.0) + 0.3*_clamp(micro_bias/10.0,0.0,2.0)
    return {"imb_now":imb2, "imb_delta":imb_delta, "micro_bias_bp":micro_bias,
            "bonus":bonus, "spread_bp":ob2["spread_bp"]}

# ===== send to Saqer (UNIFIED PAYLOAD) =====
def send_saqar(base: str):
    if not SAQAR_URL:
        tg_send("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    url = SAQAR_URL + "/hook"
    payload = {"action":"buy","coin":base.upper(),"tp_eur":TP_EUR_DEFAULT,"sl_pct":SL_PCT_DEFAULT}
    headers = {"Content-Type":"application/json"}
    if LINK_SECRET: headers["X-Link-Secret"] = LINK_SECRET
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=(6,20))
        if 200 <= r.status_code < 300:
            tg_send(f"📡 أرسلت {base} إلى صقر | {r.status_code}")
            return True
        tg_send(f"❌ فشل إرسال {base} لصقر | status={r.status_code} | {r.text[:160]}")
    except Exception as e:
        tg_send(f"❌ خطأ إرسال لصقر: {e}")
    return False

# ===== universe / scoring =====
def list_top_by_1h_volume():
    mk = _ex.load_markets()
    syms = [s for s,i in mk.items() if i.get("active",True) and i.get("quote")==QUOTE]
    syms = syms[:max(10, min(TOP_UNIVERSE, len(syms)))]
    rows = []
    for s in syms:
        o1h = fetch_ohlcv(s, "1h", 2)
        if not o1h: continue
        close = float(o1h[-1][4]); vol = float(o1h[-1][5]); q = close*vol
        rows.append((s,q)); diplomatic_sleep(REQUEST_SLEEP_MS)
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in rows[:50]]

def eval_symbol(sym: str):
    # دفتر سريع للتصفية الأولية
    ob = fetch_orderbook(sym, depth=5)
    if not ob: return None

    # شموع 1m للزخم والحجم النسبي
    o1 = fetch_ohlcv(sym, "1m", 40)
    if len(o1) < 25: return None
    lc, lv = float(o1[-1][4]), float(o1[-1][5])
    prev20 = o1[-21:-1]
    medv   = st.median([float(x[5]) for x in prev20 if float(x[5])>0]) if prev20 else 0.0
    h20    = max(float(x[2]) for x in prev20) if prev20 else lc
    brk = max((lc/max(h20,1e-12)-1)*100.0, 0.0)
    try: d1 = (lc/float(o1[-2][4])-1)*100.0
    except: d1 = 0.0
    try: d3 = (lc/float(o1[-4][4])-1)*100.0
    except: d3 = 0.0
    mom = max(d1,0.0) + 0.5*max(d3,0.0)
    vm  = max(min((lv/max(medv,1e-9)) if medv>0 else 0.0, 6.0), 0.0)

    # Orderflow لحظي (عمق أكبر + لقطتين)
    of = sample_orderflow(sym, depth=OF_DEPTH, wait_ms=OF_WAIT_MS)
    if not of: return None
    if of["imb_now"] < IMB_MIN:  # حد أدنى لاختلال الشراء
        return None

    # مكافأة دفتر الأوامر البسيطة + عقوبة السبريد
    ob_bonus = _clamp(ob["bid_imb"],0.0,2.0)*0.2
    spr_pen  = (ob["spread_bp"]/100.0)*0.2

    base_score = 1.10*brk + 0.90*vm + 0.55*mom + ob_bonus - spr_pen
    score = base_score + OF_WEIGHT * of["bonus"]

    return {"symbol":sym, "base":sym.split("/")[0], "score":score,
            "brk":brk, "vm":vm, "mom1":d1, "mom3":d3,
            "spr":ob["spread_bp"], "imb":ob["bid_imb"],
            "of_imb":of["imb_now"], "of_d":of["imb_delta"], "of_mp":of["micro_bias_bp"]}

def run_filter_and_pick():
    top_syms = list_top_by_1h_volume()
    if not top_syms: return None, []
    cands = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(eval_symbol, s): s for s in top_syms}
        for f in as_completed(futs):
            r = f.result()
            if r: cands.append(r)
            diplomatic_sleep(REQUEST_SLEEP_MS)
    if not cands: return None, []
    cands.sort(key=lambda r: r["score"], reverse=True)
    return cands[0], cands[:3]

# ===== Busy flag (wait Saqer) =====
_busy_lock = Lock()
_is_busy = False

def set_busy(v: bool):
    global _is_busy
    with _busy_lock: _is_busy = v

def is_busy():
    with _busy_lock: return _is_busy

# ===== flows =====
def do_scan_and_send():
    if is_busy():
        tg_send("⏸ مشغول حاليًا بصفقة سابقة. انتظر إشارة صقر /ready.")
        return
    set_busy(True)  # نعتبر حالنا داخل صفقة حتى لو فشل الإرسال لاحقًا (نرجع نحرّره عند الفشل)
    try:
        tg_send("🔎 بدء فلترة Best-of-50 (1h→1m + orderflow)…")
        top1, top3 = run_filter_and_pick()
        if not top1:
            set_busy(False)
            tg_send("⏸ لا يوجد مرشح مناسب (بيانات ناقصة أو شروط orderflow غير متحققة).")
            return

        if REPORT_TOP3:
            lines = [f"{i}) {r['symbol']}: sc={r['score']:.2f} | brk={r['brk']:.2f}% "
                     f"volx={r['vm']:.2f} | mom1={r['mom1']:.2f}% | spr={r['spr']:.0f}bp "
                     f"imb={r.get('of_imb', r['imb']):.2f} d={r.get('of_d',0):+.2f} mp={r.get('of_mp',0):+.0f}bp"
                     for i,r in enumerate(top3,1)]
            tg_send("🎯 Top3:\n" + "\n".join(lines))

        tg_send(f"🧠 Top1: {top1['symbol']} | score={top1['score']:.2f}")
        ok = send_saqar(top1["base"])
        if ok:
            tg_send("✅ أُرسل Top1 لصقر. بانتظار إشارة /ready…")
        else:
            tg_send("❌ فشل إرسال الطلب لصقر. أزلت حالة الانشغال.")
            set_busy(False)
    except Exception as e:
        tg_send(f"❌ خطأ أثناء الفلترة: {type(e).__name__}: {e}")
        set_busy(False)

# ===== HTTP =====
@app.route("/scan", methods=["GET"])
def scan_manual_http():
    import threading
    threading.Thread(target=do_scan_and_send, daemon=True).start()
    return jsonify(ok=True, msg="scan started"), 200

@app.route("/ready", methods=["POST"])
def on_ready():
    # صقر سيرسل: {"coin":"ADA","reason":"tp_filled|sl_triggered|buy_failed","pnl_eur":<float|null>}
    if LINK_SECRET and request.headers.get("X-Link-Secret","") != LINK_SECRET:
        return jsonify(ok=False, err="bad secret"), 401
    data = request.get_json(force=True) or {}
    coin   = (data.get("coin") or "").upper()
    reason = data.get("reason") or "-"
    pnl    = data.get("pnl_eur")
    try: pnl_txt = f"{float(pnl):.4f}€" if pnl is not None else "—"
    except: pnl_txt = "—"
    tg_send(f"✅ صقر أنهى {coin} (سبب={reason}, ربح={pnl_txt}). أصبحت جاهزًا لطلب /scan جديد.")
    set_busy(False)   # نرجع جاهزين لسكان يدوي جديد
    return jsonify(ok=True)

# ===== Telegram webhook (اختياري) =====
@app.route("/webhook", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str(msg.get("chat", {}).get("id", "")) or None
    text = (msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id): return jsonify(ok=True), 200
    if text.startswith("/scan"):
        import threading; threading.Thread(target=do_scan_and_send, daemon=True).start()
        return jsonify(ok=True), 200
    if text.startswith("/status"):
        tg_send("الحالة: " + ("مشغول ⏳" if is_busy() else "جاهز ✅")); return jsonify(ok=True), 200
    if text.startswith("/ping"):
        tg_send("pong ✅"); return jsonify(ok=True), 200
    tg_send("أوامر: /scan ، /status ، /ping"); return jsonify(ok=True), 200

@app.get("/")
def home(): return "Abosiyah — Manual Top1 + wait ready ✅", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))