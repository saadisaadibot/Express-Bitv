# -*- coding: utf-8 -*-
"""
Abosiyah — Best-of-50 (1h→1m), Soft-Score Top1 + Telegram /webhook
يرسل أمر شراء موحّد لصقر، ويستقبل ready ثم يعيد /scan تلقائياً.

ENV:
  BOT_TOKEN, CHAT_ID
  SAQAR_WEBHOOK              # مثال: https://saqer.up.railway.app  (بدون سلاش أخير)
  LINK_SECRET                # اختياري: سر مشترك للهيدر X-Link-Secret
  EXCHANGE=bitvavo, QUOTE=EUR
  TOP_UNIVERSE=120, MAX_WORKERS=6, REQUEST_SLEEP_MS=40, MAX_RPS=8, REPORT_TOP3=1
  TP_EUR_DEFAULT=0.05        # تمريرها مع كل طلب شراء (يمكن صقر يتجاهلها إذا أراد)
  SL_PCT_DEFAULT=-2
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

def fetch_orderbook(sym, depth=5):
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
    syms = syms[:max(10,min(TOP_UNIVERSE,len(syms)))]
    rows = []
    for s in syms:
        o1h = fetch_ohlcv(s, "1h", 2)
        if not o1h: continue
        close = float(o1h[-1][4]); vol = float(o1h[-1][5]); q = close*vol
        rows.append((s,q)); diplomatic_sleep(REQUEST_SLEEP_MS)
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in rows[:50]]

def eval_symbol(sym: str):
    ob = fetch_orderbook(sym, depth=5)
    if not ob: return None
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
    ob_bonus = min(max(ob["bid_imb"],0.0),2.0)*0.3
    spr_pen  = (ob["spread_bp"]/100.0)*0.2
    score = 1.15*brk + 0.85*vm + 0.6*mom + ob_bonus - spr_pen
    return {"symbol":sym,"base":sym.split("/")[0],"score":score,"brk":brk,"vm":vm,
            "mom1":d1,"mom3":d3,"spr":ob["spread_bp"],"imb":ob["bid_imb"]}

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

# ===== flows =====
def do_scan_and_send(chat_id=None):
    tg_send("🔎 بدء فلترة Best-of-50 (1h→1m)…")
    top1, top3 = run_filter_and_pick()
    if not top1:
        tg_send("⏸ لا يوجد مرشح (بيانات ناقصة)."); return
    if REPORT_TOP3:
        lines = [f"{i}) {r['symbol']}: sc={r['score']:.2f} | brk={r['brk']:.2f}% "
                 f"volx={r['vm']:.2f} | mom1={r['mom1']:.2f}% | spr={r['spr']:.0f}bp ob={r['imb']:.2f}"
                 for i,r in enumerate(top3,1)]
        tg_send("🎯 Top3:\n" + "\n".join(lines))
    tg_send(f"🧠 Top1: {top1['symbol']} | score={top1['score']:.2f}")
    ok = send_saqar(top1["base"])
    tg_send(f"📡 أرسلت {top1['base']} إلى صقر | ok={ok}")

# ===== HTTP =====
@app.route("/scan", methods=["GET"])
def scan_manual_http():
    import threading; threading.Thread(target=do_scan_and_send, daemon=True).start()
    return jsonify(ok=True, msg="scan started"), 200

@app.route("/ready", methods=["POST"])
def on_ready():
    # صقر سيرسل: {"coin":"ADA","reason":"tp_filled|sl_triggered|buy_failed","pnl_eur":null}
    if LINK_SECRET and request.headers.get("X-Link-Secret","") != LINK_SECRET:
        return jsonify(ok=False, err="bad secret"), 401
    data = request.get_json(force=True) or {}
    coin   = (data.get("coin") or "").upper()
    reason = data.get("reason") or "-"
    pnl    = data.get("pnl_eur")
    try: pnl_txt = f"{float(pnl):.4f}€" if pnl is not None else "—"
    except: pnl_txt = "—"
    tg_send(f"✅ صقر أنهى {coin} (سبب={reason}, ربح={pnl_txt}). فلترة جديدة…")
    import threading; threading.Thread(target=do_scan_and_send, daemon=True).start()
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
        import threading; threading.Thread(target=do_scan_and_send, args=(chat_id,), daemon=True).start()
        return jsonify(ok=True), 200
    if text.startswith("/ping"):
        tg_send("pong ✅"); return jsonify(ok=True), 200
    tg_send("أوامر: /scan ، /ping"); return jsonify(ok=True), 200

@app.get("/")
def home(): return "Abosiyah — Soft Top1 ✅", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))