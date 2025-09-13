# -*- coding: utf-8 -*-
"""
Abosiyah — On-Demand Signal Provider (Best-of-50, 1h→1m) + Telegram /webhook
- /scan من تيليغرام: يشغّل فلترة Best-of-50 بالخلفية ويرسل Top1 لصقر.
- /ready من صقر: عند كل خروج، يشغّل فلترة جديدة ويرسل Top1 لصقر.
- بدون جدولة تلقائية؛ كل شيء On-Demand.

ENV المطلوبة:
BOT_TOKEN, CHAT_ID, SAQAR_WEBHOOK, EXCHANGE=bitvavo, QUOTE=EUR, REQUEST_SLEEP_MS(اختياري)...
"""

import os, time, statistics as st, requests
from threading import Thread
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt

# ===== Boot / ENV =====
load_dotenv()
app = Flask(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = (os.getenv("CHAT_ID", "") or "").strip()   # لتقييد الأوامر على شات واحد
SAQAR_URL   = os.getenv("SAQAR_WEBHOOK", "").strip()

EXCHANGE    = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE       = os.getenv("QUOTE", "EUR").upper()

REQUEST_SLEEP_MS = int(os.getenv("REQUEST_SLEEP_MS", "70"))

# عتبات
MAX_SPREAD_BP    = float(os.getenv("MAX_SPREAD_BP", "35"))
MIN_BID_IMB      = float(os.getenv("MIN_BID_IMB", "1.10"))
MIN_BREAKOUT_PCT = float(os.getenv("MIN_BREAKOUT_PCT", "0.10"))
MIN_VOL_MULT     = float(os.getenv("MIN_VOL_MULT", "2.0"))

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

# ===== Utils =====
def diplomatic_sleep(ms): time.sleep(ms/1000.0)
def make_exchange(name): return getattr(ccxt, name)({"enableRateLimit": True})

def fetch_ohlcv_safe(ex, sym, tf, limit):
    try:
        return ex.fetch_ohlcv(sym, tf, limit=limit) or []
    except Exception:
        return []

def get_ob(ex, sym, depth=5):
    try:
        ob = ex.fetch_order_book(sym, limit=depth)
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

# ===== Hook إلى صقر =====
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

# ===== Core: Best-of-50 picker =====
def run_filter_and_pick():
    ex = make_exchange(EXCHANGE)
    markets = ex.load_markets()

    # A) Top50 حسب حجم آخر ساعة
    rows = []
    for sym, info in markets.items():
        try:
            if not info.get("active", True) or info.get("quote") != QUOTE:
                continue
            o1h = fetch_ohlcv_safe(ex, sym, "1h", 2)
            if not o1h: 
                continue
            last = float(o1h[-1][4])
            vol  = float(o1h[-1][5])
            qvol = vol * last
            rows.append((sym, qvol))
        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)
    rows.sort(key=lambda x: x[1], reverse=True)
    top_syms = [sym for sym,_ in rows[:50]]

    # B) تقييم 1m + دفتر أوامر
    candidates = []
    for sym in top_syms:
        try:
            ob = get_ob(ex, sym, depth=5)
            if not ob or ob["spread_bp"] > MAX_SPREAD_BP or ob["bid_imb"] < MIN_BID_IMB:
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue

            o1 = fetch_ohlcv_safe(ex, sym, "1m", 40)
            if len(o1) < 25:
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue

            last_close = float(o1[-1][4])
            last_vol   = float(o1[-1][5])
            prev20     = o1[-21:-1]
            if not prev20:
                continue
            med_vol    = st.median([float(x[5]) for x in prev20 if float(x[5])>0]) or 0.0
            if med_vol <= 0:
                continue
            high_prev20 = max(float(x[2]) for x in prev20)
            breakout_pct = (last_close / max(high_prev20, 1e-12) - 1.0) * 100.0

            # تسارع
            try: d1 = (last_close/float(o1[-2][4]) - 1)*100.0
            except: d1 = 0.0
            try: d3 = (last_close/float(o1[-4][4]) - 1)*100.0
            except: d3 = 0.0

            vol_mult = last_vol / max(med_vol, 1e-9)
            pump_guard = (d1 < 3.0 and d3 < 5.0)

            if breakout_pct >= MIN_BREAKOUT_PCT and vol_mult >= MIN_VOL_MULT and pump_guard:
                score = (
                    1.2*breakout_pct +
                    0.8*min(vol_mult, 5.0) +
                    0.3*min(ob["bid_imb"], 2.0) -
                    0.2*(ob["spread_bp"]/100.0)
                )
                base = sym.split("/")[0]
                candidates.append({
                    "score": score, "base": base, "symbol": sym,
                    "breakout_pct": breakout_pct, "vol_mult": vol_mult,
                    "spread_bp": ob["spread_bp"], "bid_imb": ob["bid_imb"]
                })
        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)

    if not candidates:
        return None

    candidates.sort(key=lambda r: r["score"], reverse=True)
    top = candidates[0]
    tg_send_text(
        f"🧠 Top1: {top['symbol']} | sc={top['score']:.2f} | "
        f"brk={top['breakout_pct']:.2f}% volx={top['vol_mult']:.2f} "
        f"spr={top['spread_bp']:.0f}bp ob={top['bid_imb']:.2f}"
    )
    return top["base"]

# ===== Handlers =====
def do_scan_and_send(chat_id=None):
    tg_send_text("🔎 بدء فلترة Best-of-50…", chat_id)
    coin = run_filter_and_pick()
    if not coin:
        tg_send_text("⏸ لا يوجد مرشح مناسب الآن.", chat_id); 
        return
    ok = send_saqar(coin)
    tg_send_text(f"📡 أرسلت {coin} إلى صقر | ok={ok}", chat_id)

@app.route("/scan", methods=["GET"])
def scan_manual_http():
    Thread(target=do_scan_and_send, daemon=True).start()
    return jsonify(ok=True, msg="scan started"), 200

@app.route("/ready", methods=["POST"])
def on_ready():
    data = request.get_json(force=True) or {}
    coin   = data.get("coin")
    reason = data.get("reason")
    pnl    = data.get("pnl_eur")
    try:
        pnl_txt = f"{float(pnl):.4f}€" if pnl is not None else "—"
    except:
        pnl_txt = "—"
    tg_send_text(f"✅ صقر أنهى {coin} (سبب={reason}, ربح={pnl_txt}). فلترة جديدة…")
    Thread(target=do_scan_and_send, daemon=True).start()
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
        Thread(target=do_scan_and_send, args=(chat_id,), daemon=True).start()
        return jsonify(ok=True), 200

    if text.startswith("/ping"):
        tg_send_text("pong ✅", chat_id); 
        return jsonify(ok=True), 200

    tg_send_text("أوامر: /scan ، /ping", chat_id)
    return jsonify(ok=True), 200

@app.route("/", methods=["GET"])
def home():
    return "Abosiyah — Best-of-50 On-Demand ✅", 200

# ===== Main =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))