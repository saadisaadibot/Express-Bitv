# -*- coding: utf-8 -*-
"""
Bot B — TopN Watcher (نسخة تلغرام + صقر)
- يستقبل CV من A ويحدّثه دون تصفير البافر أو العدادات
- يرتّب الغرفة حسب r5m ويُرسل إشعارات شراء فقط لأول ALERT_TOP_N
- لا يقصي عملة إلا إذا الغرفة ممتلئة وجاء CV أقوى
- /status يرجع تقرير مفصّل (HTTP و Telegram)
"""

import os, time, math, random, threading
from collections import deque
import requests
from flask import Flask, request, jsonify

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
BITVAVO_URL        = "https://api.bitvavo.com"
HTTP_TIMEOUT       = 8.0

ROOM_CAP           = int(os.getenv("ROOM_CAP", 24))      # حجم الغرفة
ALERT_TOP_N        = int(os.getenv("ALERT_TOP_N", 3))     # إشعارات فقط لأول N
TICK_SEC           = float(os.getenv("TICK_SEC", 2.5))    # دورة المراقبة
BATCH_SIZE         = int(os.getenv("BATCH_SIZE", 12))     # دفعة أسعار

# TTL معطّل افتراضيًا (0 = معطّل). فعّله إذا بدك خروج تلقائي بعد مدة.
TTL_MIN            = int(os.getenv("TTL_MIN", 0))         # دقائق
SPREAD_MAX_BP      = int(os.getenv("SPREAD_MAX_BP", 60))  # 0.60% حد أقصى للسبريد
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", 180))

# Telegram (اختياري)
BOT_TOKEN          = os.getenv("BOT_TOKEN", "")
CHAT_ID            = os.getenv("CHAT_ID", "")             # اختياري: لإرسال الردود لقناة ثابتة
# Webhook صقر (إلزامي لإرسال "اشتري")
SAQAR_WEBHOOK      = os.getenv("SAQAR_WEBHOOK", "")

# =========================
# 🌐 HTTP Session
# =========================
session = requests.Session()
session.headers.update({"User-Agent":"TopN-Watcher/2.0"})
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

def http_get(path, params=None, base=BITVAVO_URL, timeout=HTTP_TIMEOUT):
    try:
        r = session.get(f"{base}{path}", params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[HTTP] GET {path} failed:", e)
        return None

# =========================
# 🔔 إرسال إشعارات
# =========================
def tg_send(text, chat_id=None):
    if not BOT_TOKEN: 
        return
    cid = chat_id or CHAT_ID
    if not cid:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": cid, "text": text, "disable_web_page_preview": True}, timeout=8)
    except Exception as e:
        print("[TG] send failed:", e)

def saqar_buy(symbol):
    if not SAQAR_WEBHOOK:
        return
    payload = {"text": f"اشتري {symbol.lower()}"}   # كما طلبت: نص بسيط فقط
    try:
        r = session.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        if 200 <= r.status_code < 300:
            print(f"[SAQAR] ✅ اشتري {symbol.lower()}")
        else:
            print(f"[SAQAR] ❌ {r.status_code} {r.text[:200]}")
    except Exception as e:
        print("[SAQAR] error:", e)

# =========================
# 🧠 Coin
# =========================
class Coin:
    __slots__ = ("market","symbol","entered_at","expires_at","last_alert_at","cv","buf","last_price")
    def __init__(self, market, symbol, ttl_sec):
        t = time.time()
        self.market = market
        self.symbol = symbol
        self.entered_at = t
        # TTL اختياري — معطّل إذا TTL_MIN=0
        self.expires_at = (t + ttl_sec) if ttl_sec > 0 else 9e18
        self.last_alert_at = 0
        self.cv = {}  # r5m, r10m, volZ, spread/liq_rank…
        self.buf = deque(maxlen=int(max(600, 900/max(0.5, TICK_SEC)))))  # ~10-15 دقيقة
        self.last_price = None

    def r_change(self, seconds):
        if len(self.buf) < 2: return 0.0
        t_now, p_now = self.buf[-1]
        t_target = t_now - seconds
        base = None
        for (t,p) in reversed(self.buf):
            if t <= t_target:
                base = p; break
        if base is None: base = self.buf[0][1]
        return (p_now - base) / base * 100.0

# =========================
# 🗃️ الغرفة
# =========================
room_lock = threading.Lock()
room = {}  # market -> Coin

def ensure_coin(cv):
    """ينسخ CV بدون تصفير، ويقصي فقط عند الامتلاء إذا الجديدة أقوى."""
    m   = cv["market"]
    sym = cv.get("symbol", m.split("-")[0])
    feat= cv.get("feat", {})
    ttl_sec = int(cv.get("ttl_sec", TTL_MIN*60))

    with room_lock:
        if m in room:
            room[m].cv.update(feat)
            return

        # امتلاء؟ قص الأضعف فقط إن كانت الجديدة أقوى
        if len(room) >= ROOM_CAP:
            weakest_mk, weakest_coin = min(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0))
            if feat.get("r5m", 0.0) <= weakest_coin.cv.get("r5m", 0.0):
                return
            room.pop(weakest_mk, None)

        c = Coin(m, sym, ttl_sec)
        c.cv.update(feat)
        room[m] = c

# =========================
# 📈 الأسعار
# =========================
def fetch_price(market):
    data = http_get("/v2/ticker/price", params={"market": market})
    try:
        return float(data.get("price"))
    except:
        return None

def spread_ok(market):
    # محاولة تقدير السبريد (اختياري)
    data = http_get("/v2/ticker/24h")
    if not data: return True
    try:
        item = next((x for x in data if x.get("market")==market), None)
        if not item: return True
        bid = float(item.get("bid", 0) or 0); ask = float(item.get("ask", 0) or 0)
        if bid<=0 or ask<=0: return True
        bp = (ask - bid) / ((ask+bid)/2) * 10000
        return bp <= SPREAD_MAX_BP
    except:
        return True

# =========================
# 🔔 القرار (Top-N فقط)
# =========================
def decide_and_alert():
    nowt = time.time()
    with room_lock:
        # ترتيب حسب r5m من CV
        sorted_room = sorted(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0), reverse=True)

        top_n = sorted_room[:max(0, ALERT_TOP_N)]
        for m, c in top_n:
            if nowt - c.last_alert_at < ALERT_COOLDOWN_SEC:
                continue
            # سبريد حماية خفيفة
            if not spread_ok(m):
                continue
            c.last_alert_at = nowt
            saqar_buy(c.symbol)

# =========================
# 🩺 المراقبة
# =========================
def monitor_loop():
    rr = 0
    while True:
        try:
            with room_lock:
                markets = list(room.keys())
            if not markets:
                time.sleep(TICK_SEC); continue

            batch = markets[rr:rr+BATCH_SIZE] or markets[:BATCH_SIZE]
            rr = (rr + BATCH_SIZE) % len(markets)

            for m in batch:
                p = fetch_price(m)
                if p is None: 
                    continue
                ts = time.time()
                with room_lock:
                    c = room.get(m)
                    if not c: continue
                    c.last_price = p
                    c.buf.append((ts, p))
                    # TTL مفعّل؟ (إلا إذا TTL_MIN=0 لن يخرج)
                    if ts >= c.expires_at:
                        # ما نقصي إلا عند الامتلاء — لذا فقط مدّد قليلاً
                        c.expires_at = ts + 120  # تمديد بسيط كي يبقى حتى تأتي أقوى منه
            decide_and_alert()

        except Exception as e:
            print("[MONITOR] error:", e)
        time.sleep(TICK_SEC)

# =========================
# 📊 الحالة
# =========================
def build_status_text():
    with room_lock:
        sorted_room = sorted(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0), reverse=True)
        rows = []
        for rank, (m, c) in enumerate(sorted_room, start=1):
            r20  = c.r_change(20)
            r60  = c.r_change(60)
            r120 = c.r_change(120)
            r5m  = c.cv.get("r5m", 0.0)
            r10m = c.cv.get("r10m", 0.0)
            volZ = c.cv.get("volZ", 0.0)
            ttl  = int(c.expires_at - time.time()) if c.expires_at < 9e18 else -1
            rows.append(f"{rank:02d}. {m:<10} "
                        f"r5m={r5m:+.2f}% r10m={r10m:+.2f}% "
                        f"r20={r20:+.2f}% r60={r60:+.2f}% r120={r120:+.2f}% "
                        f"volZ={volZ:+.2f} TTL={'∞' if ttl<0 else str(ttl)+'s'}")
    hdr = f"📊 Room: {len(room)}/{ROOM_CAP} | AlertTopN={ALERT_TOP_N}"
    return hdr + ("\n" + "\n".join(rows) if rows else "\n(لا يوجد عملات بعد)")

# =========================
# 🌐 Flask API
# =========================
app = Flask(__name__)

@app.route("/")
def root():
    return "TopN Watcher B is alive ✅"

@app.route("/ingest", methods=["POST"])
def ingest():
    cv = request.get_json(force=True, silent=True) or {}
    if not cv.get("market") or not cv.get("feat"):
        return jsonify(ok=False, err="bad payload"), 400
    ensure_coin(cv)
    return jsonify(ok=True)

@app.route("/status")
def status_http():
    text = build_status_text()
    return text, 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """استقبال أوامر تلغرام — استخدم /status لعرض الحالة."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg  = data.get("message") or data.get("edited_message") or {}
        txt  = (msg.get("text") or "").strip().lower()
        chat = msg.get("chat", {}).get("id")
        if not txt: 
            return jsonify(ok=True)

        if txt in ("/status", "status", "الحالة", "/الحالة"):
            text = build_status_text()
            tg_send(text, chat_id=chat or CHAT_ID)
        return jsonify(ok=True)
    except Exception as e:
        print("[WEBHOOK] err:", e)
        return jsonify(ok=True)

# =========================
# ▶️ التشغيل
# =========================
def start_threads():
    threading.Thread(target=monitor_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)