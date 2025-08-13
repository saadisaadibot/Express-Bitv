# -*- coding: utf-8 -*-
"""
Bot B — TopN Watcher (Merged Old-Style Fetch + Decision)
- جلب أسعار سوق-بسوق على مهل (Retries + Fallback 24h ثم 1m close)
- خيط مستقل لجلب الأسعار يغذي البافر باستمرار
- خيط قرار فقط: Warmup + Nudge + Breakout + Anti-Chase + Global Gap
- يزرع price_now من A ويجدد TTL
- إشعار صقر: "اشتري {symbol}"، وتلغرام (اختياري)
- /status واضح مع ⭐ للـ TopN
"""

import os, time, threading, re
from collections import deque
import requests
from flask import Flask, request, jsonify

# =========================
# إعدادات قابلة للتعديل
# =========================
BITVAVO_URL        = "https://api.bitvavo.com"
HTTP_TIMEOUT       = 8.0

ROOM_CAP           = int(os.getenv("ROOM_CAP", 24))
ALERT_TOP_N        = int(os.getenv("ALERT_TOP_N", 3))

# قرار كل ثانية (حلقة القرار فقط)
TICK_SEC           = float(os.getenv("TICK_SEC", 1.0))

# جلب أسعار سوق-بسوق (مثل القديم)
SCAN_INTERVAL_SEC   = float(os.getenv("SCAN_INTERVAL_SEC", 5.0))   # كل كم ثانية نلف دورة كاملة على الغرفة
PER_REQUEST_GAP_SEC = float(os.getenv("PER_REQUEST_GAP_SEC", 0.12))# نوم قصير بين كل سوق وسوق
PRICE_RETRIES       = int(os.getenv("PRICE_RETRIES", 2))           # محاولات إعادة الطلب لكل سعر

TTL_MIN            = int(os.getenv("TTL_MIN", 30))       # يُجدَّد عند كل CV
SPREAD_MAX_BP      = int(os.getenv("SPREAD_MAX_BP", 60)) # 0.60%
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", 180))

# تأكيد حي + منع مطاردة
WARMUP_SEC         = int(os.getenv("WARMUP_SEC", 25))
NUDGE_R20          = float(os.getenv("NUDGE_R20", 0.12))
NUDGE_R40          = float(os.getenv("NUDGE_R40", 0.20))
BREAKOUT_BP        = float(os.getenv("BREAKOUT_BP", 6.0))   # اختراق قمة 60s
DD60_MAX           = float(os.getenv("DD60_MAX", 0.25))
GLOBAL_ALERT_GAP   = int(os.getenv("GLOBAL_ALERT_GAP", 10))
CHASE_R5M_MAX      = float(os.getenv("CHASE_R5M_MAX", 2.20))
CHASE_R20_MIN      = float(os.getenv("CHASE_R20_MIN", 0.05))

# Telegram + Saqar (اختياري)
BOT_TOKEN          = os.getenv("BOT_TOKEN", "")
CHAT_ID            = os.getenv("CHAT_ID", "")
SAQAR_WEBHOOK      = os.getenv("SAQAR_WEBHOOK", "")

# =========================
# HTTP + كاش 24h
# =========================
session = requests.Session()
session.headers.update({"User-Agent":"TopN-Watcher/Merged-Fetch"})
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

_tick24_cache = {"ts": 0.0, "data": None}
def get_24h_cached(max_age_sec: float = 2.0):
    now = time.time()
    if now - _tick24_cache["ts"] > max_age_sec:
        _tick24_cache["data"] = http_get("/v2/ticker/24h")
        _tick24_cache["ts"] = now
    return _tick24_cache["data"]

def tg_send(text, chat_id=None):
    if not BOT_TOKEN: return
    cid = chat_id or CHAT_ID
    if not cid: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": cid, "text": text, "disable_web_page_preview": True}, timeout=8)
    except Exception as e:
        print("[TG] send failed:", e)

def saqar_buy(symbol: str):
    if not SAQAR_WEBHOOK: return
    payload = {"text": f"اشتري {symbol.lower()}"}
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
    __slots__ = ("market","symbol","entered_at","expires_at","last_alert_at",
                 "cv","buf","last_price","armed_at","silent_until","price_fail")
    def __init__(self, market, symbol, ttl_sec):
        t = time.time()
        self.market = market
        self.symbol = symbol
        self.entered_at = t
        self.expires_at = t + ttl_sec
        self.last_alert_at = 0.0
        self.cv = {}
        self.buf = deque(maxlen=1200)  # ~20 دقيقة لو قرأنا عيّنة/ثانية
        self.last_price = None
        self.armed_at = t
        self.silent_until = t + WARMUP_SEC
        self.price_fail = 0

    def r_change(self, seconds: int) -> float:
        if len(self.buf) < 2: return 0.0
        t_now, p_now = self.buf[-1]
        t_target = t_now - seconds
        base = None
        for (tt,pp) in reversed(self.buf):
            if tt <= t_target:
                base = pp; break
        if base is None: base = self.buf[0][1]
        return (p_now - base) / base * 100.0

# =========================
# الغرفة
# =========================
room_lock = threading.Lock()
room = {}  # market -> Coin
VALID_MKT = re.compile(r"^[A-Z0-9]{1,10}-EUR$")

def is_valid_market(m): return bool(VALID_MKT.match(m or ""))

def ensure_coin(cv):
    """تحديث/إضافة عملة: يزرع السعر فورًا ويجدد TTL ويطبّق WARMUP."""
    m   = cv["market"].upper()
    if not is_valid_market(m): return
    sym = cv.get("symbol", m.split("-")[0])
    feat= cv.get("feat", {})
    ttl_sec = max(60, int(cv.get("ttl_sec", TTL_MIN*60)))
    nowt = time.time()

    with room_lock:
        c = room.get(m)
        if c:
            c.cv.update(feat)
            p0 = float(feat.get("price_now") or 0.0)
            if p0 > 0:
                c.last_price = p0
                c.buf.append((nowt, p0))
            c.expires_at   = nowt + TTL_MIN*60
            c.armed_at     = nowt
            c.silent_until = nowt + WARMUP_SEC
            return

        if len(room) >= ROOM_CAP:
            weakest_mk, weakest_coin = min(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0))
            if float(feat.get("r5m", 0.0)) <= float(weakest_coin.cv.get("r5m", 0.0)):
                return
            room.pop(weakest_mk, None)

        c = Coin(m, sym, ttl_sec)
        c.cv.update(feat)
        p0 = float(feat.get("price_now") or 0.0)
        if p0 > 0:
            c.last_price = p0
            c.buf.append((nowt, p0))
        room[m] = c

# =========================
# جلب الأسعار (مثل القديم) + Fallback
# =========================
_price_fail = {}          # market -> consecutive fails
_last_candle_fetch = {}   # market -> ts آخر جلب شموع

def get_price_one(market):
    """
    يقرأ /v2/ticker/price لسوق واحد. يتعامل مع dict أو list.
    Retries خفيفة + fallback 24h ثم close من شموع 1m.
    """
    p = None
    for _ in range(PRICE_RETRIES):
        data = http_get("/v2/ticker/price", params={"market": market})
        try:
            if isinstance(data, dict):
                p = float(data.get("price"))
            elif isinstance(data, list) and data:
                if len(data) == 1:
                    p = float((data[0] or {}).get("price"))
                else:
                    row = next((x for x in data if x.get("market") == market), None)
                    if row: p = float(row.get("price"))
        except Exception:
            p = None
        if p is not None and p > 0:
            return p
        time.sleep(0.1)  # backoff صغير بين المحاولات

    # Fallback 1: 24h last من الكاش
    data24 = get_24h_cached(max_age_sec=1.0)
    if data24:
        try:
            it = next((x for x in data24 if x.get("market")==market), None)
            p = float((it or {}).get("last", 0) or 0)
            if p > 0:
                return p
        except Exception:
            pass

    # Fallback 2: close من شموع 1m (مرّة كل 10s لنفس السوق)
    now = time.time()
    if now - _last_candle_fetch.get(market, 0) >= 10:
        cnd = http_get(f"/v2/{market}/candles", params={"interval":"1m", "limit": 1})
        _last_candle_fetch[market] = now
        try:
            if isinstance(cnd, list) and cnd:
                p = float(cnd[-1][4])  # close
                if p > 0:
                    return p
        except Exception:
            pass

    return None

def price_poller_loop():
    """
    يلف على غرفة الأسواق كل SCAN_INTERVAL_SEC.
    بين كل سوق وسوق PER_REQUEST_GAP_SEC لتجنب 429/400.
    يغذي البافر فقط. لا قرارات هنا.
    """
    while True:
        start = time.time()
        with room_lock:
            markets = [m for m in room.keys() if is_valid_market(m)]
        if not markets:
            time.sleep(0.5)
            continue

        for m in markets:
            p = get_price_one(m)
            if p is None or p <= 0:
                with room_lock:
                    c = room.get(m)
                    if c:
                        c.price_fail = getattr(c, "price_fail", 0) + 1
                        if c.price_fail % 5 == 0:
                            print(f"[PRICE] {m} failed {c.price_fail}x")
                time.sleep(PER_REQUEST_GAP_SEC)
                continue

            ts = time.time()
            with room_lock:
                c = room.get(m)
                if not c:
                    continue
                c.price_fail = 0
                c.last_price = p
                c.buf.append((ts, p))
                if TTL_MIN > 0 and ts >= c.expires_at:
                    c.expires_at = ts + 120

            time.sleep(PER_REQUEST_GAP_SEC)

        # حافظ على الفترة الكلية للدورة
        elapsed = time.time() - start
        time.sleep(max(0.05, SCAN_INTERVAL_SEC - elapsed))

# =========================
# القرار + الإشعار
# =========================
def spread_ok(market):
    data = get_24h_cached()
    if not data: return True
    it = next((x for x in data if x.get("market")==market), None)
    if not it: return True
    try:
        bid = float(it.get("bid", 0) or 0); ask = float(it.get("ask", 0) or 0)
        if bid<=0 or ask<=0: return True
        bp = (ask - bid) / ((ask+bid)/2) * 10000
        return bp <= SPREAD_MAX_BP
    except Exception:
        return True

def recent_high(c: Coin, seconds: int):
    if not c.buf: return None
    t_now = c.buf[-1][0]
    vals = [p for (t,p) in c.buf if t >= t_now - seconds]
    return max(vals) if vals else None

def recent_dd_pct(c: Coin, seconds: int):
    if len(c.buf) < 2: return 0.0
    t_now = c.buf[-1][0]
    sub = [(t,p) for (t,p) in c.buf if t >= t_now - seconds]
    if not sub: return 0.0
    hi = max(p for _,p in sub); last = sub[-1][1]
    return (hi - last) / hi * 100.0

last_global_alert = 0.0
def decide_and_alert():
    """يطلق فقط لأول N عملة بعد تحقق النخزة وكسر القمة وفلتر anti-chase."""
    global last_global_alert
    nowt = time.time()

    with room_lock:
        sorted_room = sorted(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0), reverse=True)
        top_n = sorted_room[:max(0, ALERT_TOP_N)]

        for m, c in top_n:
            if nowt < c.silent_until:
                continue
            if nowt - c.last_alert_at < ALERT_COOLDOWN_SEC:
                continue
            if nowt - last_global_alert < GLOBAL_ALERT_GAP:
                continue

            r5m = float(c.cv.get("r5m", 0.0))
            r20 = c.r_change(20)
            # مانع مطاردة: تمدد قوي بدون زخم حيّ
            if r5m >= CHASE_R5M_MAX and r20 < CHASE_R20_MIN:
                continue

            r40  = c.r_change(40)
            dd60 = recent_dd_pct(c, 60)
            hi60 = recent_high(c, 60)
            price_now = c.last_price
            if price_now is None or hi60 is None:
                continue

            breakout_ok = (price_now > hi60 * (1.0 + BREAKOUT_BP/10000.0))
            nudge_ok    = (r20 >= NUDGE_R20 and r40 >= NUDGE_R40)
            dd_ok       = (dd60 <= DD60_MAX)

            # تسهيل طفيف إذا A أرسل preburst
            preburst = bool((c.cv or {}).get("preburst", False))
            if preburst:
                nudge_ok = (r20 >= max(0.08, NUDGE_R20-0.04) and r40 >= max(0.14, NUDGE_R40-0.06))
                breakout_ok = (price_now > hi60 * 1.0003)

            if not (nudge_ok and breakout_ok and dd_ok):
                continue
            if not spread_ok(m):
                continue

            c.last_alert_at = nowt
            last_global_alert = nowt
            saqar_buy(c.symbol)

# =========================
# حلقة القرار فقط
# =========================
def monitor_loop():
    """
    حلقة القرار فقط: ما بتجيب أسعار.
    تعتمد على البافر الذي يملأه price_poller_loop.
    """
    while True:
        try:
            decide_and_alert()
            if int(time.time()) % 30 == 0:
                with room_lock:
                    filled = sum(1 for c in room.values() if len(c.buf) >= 2)
                print(f"[MONITOR] decision-only — buffers ok {filled}/{len(room)}")
        except Exception as e:
            print("[MONITOR] error:", e)
        time.sleep(TICK_SEC)

# =========================
# حالة وواجهات
# =========================
def build_status_text():
    with room_lock:
        sorted_room = sorted(room.items(), key=lambda kv: kv[1].cv.get("r5m", 0.0), reverse=True)
        lines = []
        for rank, (m, c) in enumerate(sorted_room, start=1):
            star = "⭐" if rank <= ALERT_TOP_N else " "
            r5m  = c.cv.get("r5m", 0.0); r10m = c.cv.get("r10m", 0.0); vz = c.cv.get("volZ", 0.0)
            r20  = c.r_change(20); r60 = c.r_change(60); r120 = c.r_change(120)
            ttl  = int(c.expires_at - time.time())
            ttl_text = "∞" if TTL_MIN == 0 else f"{ttl}s"
            lines.append(
                f"{rank:02d}.{star} {m:<10} | r5m {r5m:+.2f}%  r10m {r10m:+.2f}%  "
                f"r20 {r20:+.2f}%  r60 {r60:+.2f}%  r120 {r120:+.2f}%  volZ {vz:+.2f}  TTL {ttl_text}"
            )
    header = f"📊 Room {len(room)}/{ROOM_CAP} | TopN={ALERT_TOP_N} | Gap={GLOBAL_ALERT_GAP}s"
    return header + ("\n" + "\n".join(lines) if lines else "\n(لا يوجد عملات بعد)")

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
    return build_status_text(), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg  = data.get("message") or data.get("edited_message") or {}
        txt  = (msg.get("text") or "").strip().lower()
        chat = msg.get("chat", {}).get("id")
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
    threading.Thread(target=price_poller_loop, daemon=True).start()  # الجالب
    threading.Thread(target=monitor_loop, daemon=True).start()       # المقرِّر

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)