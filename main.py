# -*- coding: utf-8 -*-
"""
Bot B — الحاضنة الوحشية (Top N Watcher)
- يستقبل CV من A (Top 5m)
- يراقب ويحسب مؤشرات لحظية
- يرسل إشارات شراء فقط لأول ALERT_TOP_N عملة
- لا يقصي العملة إلا إذا الغرفة ممتلئة وجاءت عملة أفضل
- /status يعرض العملات مرتبة مع كامل العدادات
"""

import os, time, math, json, random, threading
from collections import deque
import requests
from flask import Flask, request, jsonify

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
BITVAVO_URL    = "https://api.bitvavo.com"
HTTP_TIMEOUT   = 8.0

ROOM_CAP       = 24          # أقصى عدد عملات تحت المراقبة
TTL_MIN        = 30          # مدة بقاء الرمز (دقائق)
STICKY_MIN     = 5           # فترة سماح بعد الدخول (دقائق)
TICK_SEC       = 2.5         # دورة المراقبة (ثواني)
BATCH_SIZE     = 12          # عدد الأسواق بكل دفعة

ALERT_TOP_N    = 3           # يرسل إشعارات فقط لأول N عملة بالأداء

SPREAD_MAX_BP  = 30          # سبريد أقصى (0.30%)
ALERT_COOLDOWN_SEC = 120     # كولداون بين الإشعارات لنفس العملة

SAQAR_WEBHOOK  = os.getenv("SAQAR_WEBHOOK", "")  # رابط صقر

# =========================
# 🌐 HTTP
# =========================
session = requests.Session()
session.headers.update({"User-Agent":"TopN-Watcher/1.0"})
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

def saqar_buy(symbol):
    if not SAQAR_WEBHOOK: 
        return
    payload = {"text": f"اشتري {symbol.upper()}"}
    try:
        r = session.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        if 200 <= r.status_code < 300:
            print(f"[SAQAR] ✅ Sent buy {symbol.upper()}")
        else:
            print(f"[SAQAR] ❌ Failed {r.status_code}")
    except Exception as e:
        print("[SAQAR] error:", e)

# =========================
# 🧠 كائن العملة
# =========================
class Coin:
    __slots__ = (
        "market","symbol","entered_at","expires_at","sticky_until",
        "last_alert_at","cv","buf","last_price"
    )
    def __init__(self, market, symbol, ttl_sec):
        nowt = time.time()
        self.market = market
        self.symbol = symbol
        self.entered_at = nowt
        self.expires_at = nowt + ttl_sec
        self.sticky_until = nowt + STICKY_MIN*60
        self.last_alert_at = 0
        self.cv = {}
        self.buf = deque(maxlen=int(max(600, 900/TICK_SEC)))
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
    m = cv["market"]
    sym = cv.get("symbol", m.split("-")[0])
    ttl_sec = int(cv.get("ttl_sec", TTL_MIN*60))
    with room_lock:
        if m in room:
            # تحديث بيانات CV بدون تصفير البافر
            room[m].cv.update(cv.get("feat", {}))
            return
        # إذا الغرفة ممتلئة → أقصِ أضعف إذا CV الجديد أقوى
        if len(room) >= ROOM_CAP:
            weakest = min(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0))
            if cv["feat"].get("r5m", 0.0) > weakest[1].cv.get("r5m", 0.0):
                room.pop(weakest[0], None)
            else:
                return
        # إضافة عملة جديدة
        c = Coin(m, sym, ttl_sec)
        c.cv.update(cv.get("feat", {}))
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

# =========================
# 🔔 القرار
# =========================
def decide_and_alert():
    nowt = time.time()
    with room_lock:
        # ترتيب العملات حسب r5m (من CV)
        sorted_room = sorted(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0), reverse=True)
        for rank, (m, c) in enumerate(sorted_room, start=1):
            if rank > ALERT_TOP_N:
                continue
            if nowt - c.last_alert_at < ALERT_COOLDOWN_SEC:
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
                if p is None: continue
                ts = time.time()
                with room_lock:
                    c = room.get(m)
                    if not c: continue
                    c.last_price = p
                    c.buf.append((ts, p))
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
            r20 = c.r_change(20)
            r60 = c.r_change(60)
            r120 = c.r_change(120)
            r5m = c.cv.get("r5m", 0.0)
            r10m = c.cv.get("r10m", 0.0)
            volZ = c.cv.get("volZ", 0.0)
            ttl  = int(c.expires_at - time.time())
            rows.append(f"{rank:02d}. {m:<10} r5m={r5m:+.2f}% r10m={r10m:+.2f}% "
                        f"r20={r20:+.2f}% r60={r60:+.2f}% r120={r120:+.2f}% "
                        f"volZ={volZ:+.2f} TTL={ttl}s")
    hdr = f"📊 Room: {len(room)}/{ROOM_CAP} | AlertTopN={ALERT_TOP_N}"
    return hdr + "\n" + "\n".join(rows)

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
def status():
    return build_status_text(), 200, {"Content-Type":"text/plain; charset=utf-8"}

# =========================
# ▶️ التشغيل
# =========================
def start_threads():
    threading.Thread(target=monitor_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)