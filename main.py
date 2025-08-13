# -*- coding: utf-8 -*-
"""
Bot B — TopN Watcher (Telegram + Saqar)
- ينسخ CV من A ويكمل المراقبة بدون تصفير البافر
- يجدد TTL للرمز إلى 30 دقيقة عند وصول CV جديد لنفس الرمز
- لا يقصي عملة إلا إذا الغرفة ممتلئة وجاءت أقوى منها
- يرسل لصقر "اشتري {symbol}" فقط لأول ALERT_TOP_N عملات
- /status عبر HTTP وتلغرام
"""

import os, time, threading, random
from collections import deque
import requests
from flask import Flask, request, jsonify

# =========================
# إعدادات قابلة للتعديل
# =========================
BITVAVO_URL        = "https://api.bitvavo.com"
HTTP_TIMEOUT       = 8.0

ROOM_CAP           = int(os.getenv("ROOM_CAP", 24))
ALERT_TOP_N        = int(os.getenv("ALERT_TOP_N", 3))        # إشعار فقط لتوب N
TICK_SEC           = float(os.getenv("TICK_SEC", 2.5))
BATCH_SIZE         = int(os.getenv("BATCH_SIZE", 12))
TTL_MIN            = int(os.getenv("TTL_MIN", 30))           # مدة المراقبة (دقائق)
SPREAD_MAX_BP      = int(os.getenv("SPREAD_MAX_BP", 60))     # 0.60%
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", 180))

# Telegram (اختياري)
BOT_TOKEN          = os.getenv("BOT_TOKEN", "")
CHAT_ID            = os.getenv("CHAT_ID", "")                 # افتراضي لإرسال /status

# Webhook صقر (إلزامي لإرسال "اشتري")
SAQAR_WEBHOOK      = os.getenv("SAQAR_WEBHOOK", "")

# =========================
# HTTP
# =========================
session = requests.Session()
session.headers.update({"User-Agent":"TopN-Watcher/2.1"})
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

def tg_send(text, chat_id=None):
    if not BOT_TOKEN: return
    cid = chat_id or CHAT_ID
    if not cid: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": cid, "text": text, "disable_web_page_preview": True}, timeout=8)
    except Exception as e:
        print("[TG] send failed:", e)

def saqar_buy(symbol):
    if not SAQAR_WEBHOOK: return
    payload = {"text": f"اشتري {symbol.lower()}"}  # رسمي وبسيط
    try:
        r = session.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        if 200 <= r.status_code < 300:
            print(f"[SAQAR] ✅ اشتري {symbol.lower()}")
        else:
            print(f"[SAQAR] ❌ {r.status_code} {r.text[:160]}")
    except Exception as e:
        print("[SAQAR] error:", e)

# =========================
# Coin
# =========================
class Coin:
    __slots__ = ("market","symbol","entered_at","expires_at","last_alert_at","cv","buf","last_price")
    def __init__(self, market, symbol, ttl_sec):
        t = time.time()
        self.market = market
        self.symbol = symbol
        self.entered_at = t
        self.expires_at = t + ttl_sec
        self.last_alert_at = 0
        self.cv = {}  # r5m, r10m, volZ, liq_rank ..
        # ~10-15 دقيقة بافر
        self.buf = deque(maxlen=int(max(600, 900/max(0.5, TICK_SEC))))
        self.last_price = None

    def r_change(self, seconds: int) -> float:
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
# الغرفة
# =========================
room_lock = threading.Lock()
room = {}  # market -> Coin

def ensure_coin(cv):
    """
    - إذا الرمز موجود: حدّث CV وجدد TTL إلى 30 دقيقة من الآن (أو حسب TTL_MIN).
    - إذا غير موجود:
        * إن كانت الغرفة ممتلئة: اقصِ الأضعف فقط إن كانت الجديدة أقوى.
        * ثم أضف الرمز.
    """
    m   = cv["market"]
    sym = cv.get("symbol", m.split("-")[0])
    feat= cv.get("feat", {})
    ttl_sec = max(60, int(cv.get("ttl_sec", TTL_MIN*60)))  # ضمان ≥ 60s

    nowt = time.time()
    with room_lock:
        c = room.get(m)
        if c:
            c.cv.update(feat)
            # ✅ تجديد الوقت المتبقي إلى 30 دقيقة من الآن
            c.expires_at = nowt + TTL_MIN*60
            return

        # الغرفة ممتلئة؟
        if len(room) >= ROOM_CAP:
            weakest_mk, weakest_coin = min(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0))
            if feat.get("r5m", 0.0) <= weakest_coin.cv.get("r5m", 0.0):
                return
            room.pop(weakest_mk, None)

        c = Coin(m, sym, ttl_sec)
        c.cv.update(feat)
        room[m] = c

# =========================
# الأسعار + سبريد
# =========================
def fetch_price(market):
    data = http_get("/v2/ticker/price", params={"market": market})
    try:
        return float(data.get("price"))
    except:
        return None

def spread_ok(market) -> bool:
    data = http_get("/v2/ticker/24h")
    if not data: return True
    it = next((x for x in data if x.get("market")==market), None)
    if not it: return True
    try:
        bid = float(it.get("bid", 0) or 0); ask = float(it.get("ask", 0) or 0)
        if bid<=0 or ask<=0: return True
        bp = (ask - bid) / ((ask+bid)/2) * 10000
        return bp <= SPREAD_MAX_BP
    except:
        return True

# =========================
# القرار (Top-N فقط)
# =========================
def decide_and_alert():
    nowt = time.time()
    with room_lock:
        sorted_room = sorted(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0), reverse=True)
        top_n = sorted_room[:max(0, ALERT_TOP_N)]
        for m, c in top_n:
            if nowt - c.last_alert_at < ALERT_COOLDOWN_SEC:
                continue
            if not spread_ok(m):
                continue
            c.last_alert_at = nowt
            saqar_buy(c.symbol)

# =========================
# المراقبة
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
                    # خروج تلقائي فقط إذا فعّال TTL_MIN>0
                    if TTL_MIN > 0 and ts >= c.expires_at:
                        # لا نقصي فورًا: نمدد قليلًا ريثما تأتي أقوى منه (منطقك)
                        c.expires_at = ts + 120
            decide_and_alert()
        except Exception as e:
            print("[MONITOR] error:", e)
        time.sleep(TICK_SEC)

# =========================
# الحالة
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
            ttl  = int(c.expires_at - time.time())
            ttl_text = "∞" if TTL_MIN == 0 else (str(ttl)+"s")
            rows.append(f"{rank:02d}. {m:<10} "
                        f"r5m={r5m:+.2f}% r10m={r10m:+.2f}% "
                        f"r20={r20:+.2f}% r60={r60:+.2f}% r120={r120:+.2f}% "
                        f"volZ={volZ:+.2f} TTL={ttl_text}")
    hdr = f"📊 Room: {len(room)}/{ROOM_CAP} | AlertTopN={ALERT_TOP_N}"
    return hdr + ("\n" + "\n".join(rows) if rows else "\n(لا يوجد عملات بعد)")

# =========================
# Flask API
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
    """استخدم /status في تلغرام لعرض الحالة."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg  = data.get("message") or data.get("edited_message") or {}
        txt  = (msg.get("text") or "").strip().lower()
        chat = msg.get("chat", {}).get("id")
        if not txt: 
            return jsonify(ok=True)
        if txt in ("/status", "status", "الحالة", "/الحالة"):
            tg_send(build_status_text(), chat_id=chat or CHAT_ID)
        return jsonify(ok=True)
    except Exception as e:
        print("[WEBHOOK] err:", e)
        return jsonify(ok=True)

# =========================
# التشغيل
# =========================
def start_threads():
    threading.Thread(target=monitor_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)