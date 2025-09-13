# -*- coding: utf-8 -*-
"""
Abosiyah — Best-of-50 (1h→1m), Soft-Score Top1 + Telegram /webhook

- الفلترة الأولى: Top50 حسب *حجم آخر ساعة* (EUR) لكل أزواج EUR.
- تقييم متوازي (خيوط محدودة) لكل زوج:
  breakout 1m فوق High(20) الماضي (القيم السالبة تُقص إلى 0)
  زخم 1m و3m (clamped كبونص)
  مضاعف حجم 1m مقابل ميديان آخر 20 (clamped)
  جودة دفتر الأوامر: imbalance كبونص، spread كعقوبة
- لا قيود صلبة: نحسب score للجميع ونختار الأعلى دائمًا.
- /webhook (تيليغرام): أوامر /scan و /ping.
- /scan (HTTP): يشغّل فحص بالخلفية.
- /ready (من صقر): عند كل خروج، نعمل فحص ونرسل Top1 جديد.

ENV:
  BOT_TOKEN, CHAT_ID, SAQAR_WEBHOOK
  EXCHANGE=bitvavo, QUOTE=EUR
  TOP_UNIVERSE=120         # كم زوج نفحص من ماركت EUR قبل اختيار Top50 ساعة
  MAX_WORKERS=6            # عدد الخيوط المتوازية (آمن 4-8)
  REQUEST_SLEEP_MS=40      # نوم خفيف داخل الخيوط
  MAX_RPS=8                # أقصى طلبات/ثانية (Throttle عام)
  REPORT_TOP3=1            # إظهار Top3 في رسالة تيليغرام
"""

import os, time, math, statistics as st, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = (os.getenv("CHAT_ID", "") or "").strip()
SAQAR_URL   = os.getenv("SAQAR_WEBHOOK", "").strip()

EXCHANGE    = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE       = os.getenv("QUOTE", "EUR").upper()

TOP_UNIVERSE      = int(os.getenv("TOP_UNIVERSE", "120"))
MAX_WORKERS       = max(1, int(os.getenv("MAX_WORKERS", "6")))
REQUEST_SLEEP_MS  = int(os.getenv("REQUEST_SLEEP_MS", "40"))
MAX_RPS           = float(os.getenv("MAX_RPS", "8"))
REPORT_TOP3       = int(os.getenv("REPORT_TOP3", "1"))

# ===== Telegram =====
def tg_send_text(text, chat_id=None):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id or CHAT_ID, "text": text},
            timeout=8
        )
    except Exception as e:
        print("tg_send error:", e)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

# ===== Exchange + Throttle =====
def make_exchange(name):
    return getattr(ccxt, name)({"enableRateLimit": True})

_ex = make_exchange(EXCHANGE)

_th_lock = Lock()
_last_ts = 0.0
_min_dt  = 1.0 / max(0.1, MAX_RPS)

def throttle():
    """حارس بسيط لعدد الطلبات/ثانية على مستوى العملية."""
    global _last_ts
    with _th_lock:
        now = time.time()
        wait = _min_dt - (now - _last_ts)
        if wait > 0: time.sleep(wait)
        _last_ts = time.time()

def diplomatic_sleep(ms): 
    if ms>0: time.sleep(ms/1000.0)

# ===== Safe fetchers =====
def fetch_ohlcv(sym, tf, limit):
    try:
        throttle()
        return _ex.fetch_ohlcv(sym, tf, limit=limit) or []
    except Exception:
        return []

def fetch_orderbook(sym, depth=5):
    try:
        throttle()
        ob = _ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return None
        bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
        spread_bp = (ask - bid)/((ask + bid)/2) * 10000.0
        bidvol = sum(float(x[1]) for x in ob["bids"][:depth])
        askvol = sum(float(x[1]) for x in ob["asks"][:depth])
        bid_imb = bidvol / max(askvol, 1e-9)
        return {"bid": bid, "ask": ask, "spread_bp": spread_bp, "bid_imb": bid_imb}
    except Exception:
        return None

# ===== Saqar hook =====
def send_saqar(base: str):
    if not SAQAR_URL:
        tg_send_text("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    url = SAQAR_URL.rstrip("/") + "/hook"
    payload = {"cmd": "buy", "coin": base.upper(), "ts": int(time.time()*1000), "ttl": 60}
    try:
        r = requests.post(url, json=payload, timeout=(6,20))
        if 200 <= r.status_code < 300:
            tg_send_text(f"📡 أرسلت {base} إلى صقر | resp={r.text[:200]}")
            return True
        tg_send_text(f"❌ فشل إرسال {base} لصقر | status={r.status_code}")
    except Exception as e:
        tg_send_text(f"❌ خطأ إرسال لصقر: {e}")
    return False

# ===== Universe (1h volume) =====
def list_top_by_1h_volume():
    """يرجع Top50 حسب (baseVolume_1h * close_1h) داخل أفضل TOP_UNIVERSE من أزواج EUR."""
    markets = _ex.load_markets()
    syms = [sym for sym,info in markets.items() if info.get("active",True) and info.get("quote")==QUOTE]
    # قلص إلى TOP_UNIVERSE أولاً لتخفيف الضغط
    syms = syms[:max(10, min(TOP_UNIVERSE, len(syms)))]
    rows = []
    for sym in syms:
        o1h = fetch_ohlcv(sym, "1h", 2)
        if not o1h: 
            continue
        close = float(o1h[-1][4]); vol = float(o1h[-1][5])
        qvol  = close * vol
        rows.append((sym, qvol))
        diplomatic_sleep(REQUEST_SLEEP_MS)
    rows.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym,_ in rows[:50]]

# ===== Scoring (soft) =====
def eval_symbol(sym: str) -> dict | None:
    """
    يرجّع dict يحتوي score + المقاييس. لا يرمي مرشحين بسبب شروط صلبة.
    يعيد None إذا البيانات ناقصة (OB أو OHLCV 1m).
    """
    ob = fetch_orderbook(sym, depth=5)
    if not ob: 
        return None

    o1 = fetch_ohlcv(sym, "1m", 40)
    if len(o1) < 25: 
        return None

    last_close = float(o1[-1][4])
    last_vol   = float(o1[-1][5])
    prev20     = o1[-21:-1]
    med_vol    = st.median([float(x[5]) for x in prev20 if float(x[5])>0]) if prev20 else 0.0
    high_prev20 = max(float(x[2]) for x in prev20) if prev20 else last_close

    # مكوّنات السكور (soft)
    breakout_pct = (last_close / max(high_prev20, 1e-12) - 1.0) * 100.0
    br = max(breakout_pct, 0.0)                  # لا نعاقب الاختراق السلبي

    try: d1 = (last_close/float(o1[-2][4]) - 1)*100.0
    except: d1 = 0.0
    try: d3 = (last_close/float(o1[-4][4]) - 1)*100.0
    except: d3 = 0.0
    mom = max(d1, 0.0) + 0.5*max(d3, 0.0)        # زخم قصير + جزء من زخم 3m (soft)

    vol_mult = (last_vol / max(med_vol, 1e-9)) if med_vol>0 else 0.0
    vm = max(min(vol_mult, 6.0), 0.0)            # سقف 6×

    ob_bonus  = min(max(ob["bid_imb"], 0.0), 2.0) * 0.3
    spr_pen   = (ob["spread_bp"]/100.0) * 0.2    # 20% من نسبة السبريد

    # السكور النهائي (قابِل للضبط لاحقًا)
    score = 1.15*br + 0.85*vm + 0.6*mom + ob_bonus - spr_pen

    base = sym.split("/")[0]
    return {
        "symbol": sym, "base": base, "score": score,
        "brk": breakout_pct, "vm": vol_mult, "mom1": d1, "mom3": d3,
        "spr": ob["spread_bp"], "imb": ob["bid_imb"]
    }

# ===== Core: pick Top1 =====
def run_filter_and_pick():
    # 1) Top50 حسب حجم الساعة
    top_syms = list_top_by_1h_volume()
    if not top_syms:
        return None, []

    # 2) تقييم متوازي
    candidates = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(eval_symbol, sym): sym for sym in top_syms}
        for fut in as_completed(futs):
            res = fut.result()
            if res: candidates.append(res)
            diplomatic_sleep(REQUEST_SLEEP_MS)

    if not candidates:
        return None, []

    candidates.sort(key=lambda r: r["score"], reverse=True)
    top1 = candidates[0]
    return top1, candidates[:3]  # نرجع Top3 للاستعراض

# ===== Handlers =====
def do_scan_and_send(chat_id=None):
    tg_send_text("🔎 بدء فلترة Best-of-50 (1h→1m)…", chat_id)
    top1, top3 = run_filter_and_pick()
    if not top1:
        tg_send_text("⏸ لا يوجد مرشح (بيانات ناقصة).", chat_id); 
        return

    if REPORT_TOP3:
        lines = []
        for i, r in enumerate(top3, 1):
            lines.append(f"{i}) {r['symbol']}: sc={r['score']:.2f} | brk={r['brk']:.2f}% "
                         f"volx={r['vm']:.2f} | mom1={r['mom1']:.2f}% | spr={r['spr']:.0f}bp ob={r['imb']:.2f}")
        tg_send_text("🎯 Top3:\n" + "\n".join(lines), chat_id)

    tg_send_text(f"🧠 Top1: {top1['symbol']} | score={top1['score']:.2f}", chat_id)
    ok = send_saqar(top1["base"])
    tg_send_text(f"📡 أرسلت {top1['base']} إلى صقر | ok={ok}", chat_id)

@app.route("/scan", methods=["GET"])
def scan_manual_http():
    import threading
    threading.Thread(target=do_scan_and_send, daemon=True).start()
    return jsonify(ok=True, msg="scan started"), 200

@app.route("/ready", methods=["POST"])
def on_ready():
    data = request.get_json(force=True) or {}
    coin   = data.get("coin")
    reason = data.get("reason")
    pnl    = data.get("pnl_eur")
    try: pnl_txt = f"{float(pnl):.4f}€" if pnl is not None else "—"
    except: pnl_txt = "—"
    tg_send_text(f"✅ صقر أنهى {coin} (سبب={reason}, ربح={pnl_txt}). فلترة جديدة…")
    import threading
    threading.Thread(target=do_scan_and_send, daemon=True).start()
    return jsonify(ok=True)

# ===== Telegram Webhook =====
@app.route("/webhook", methods=["POST"])
def tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str(msg.get("chat", {}).get("id", "")) or None
    text = (msg.get("text") or "").strip()

    if not chat_id or (not _auth_chat(chat_id)):
        return jsonify(ok=True), 200

    if text.startswith("/scan"):
        tg_send_text("⏳ جارٍ الفحص بالخلفية…", chat_id)
        import threading
        threading.Thread(target=do_scan_and_send, args=(chat_id,), daemon=True).start()
        return jsonify(ok=True), 200

    if text.startswith("/ping"):
        tg_send_text("pong ✅", chat_id); 
        return jsonify(ok=True), 200

    tg_send_text("أوامر: /scan ، /ping", chat_id)
    return jsonify(ok=True), 200

@app.route("/", methods=["GET"])
def home():
    return "Abosiyah — Best-of-50 (1h→1m) — Soft Top1 ✅", 200

# ===== Main =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))