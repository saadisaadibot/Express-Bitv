# -*- coding: utf-8 -*-
"""
Bot B — TopN Watcher (Redis History + Robust Pricing + Live Re-Ranking)
- ترتيب الغرفة لحظيًا: score = r5_live + 0.7*r10_live (من Redis أو البافر)
- يمسح Redis (px:*) مرة واحدة عند الإقلاع
- باقي المنطق كما هو: جلب سعر عنيد، history في Redis، إشعارات لصقر، /status و /diag
"""

import os, time, threading, re
from collections import deque
import requests, redis
from flask import Flask, request, jsonify

# =========================
# إعدادات
# =========================
BITVAVO_URL          = "https://api.bitvavo.com"
HTTP_TIMEOUT         = 8.0

ROOM_CAP             = int(os.getenv("ROOM_CAP", 24))
ALERT_TOP_N          = int(os.getenv("ALERT_TOP_N", 10))  # الافتراضي 10

# حلقات
TICK_SEC             = float(os.getenv("TICK_SEC", 1.0))           # قرار
SCAN_INTERVAL_SEC    = float(os.getenv("SCAN_INTERVAL_SEC", 2.0))  # سحب أسعار
PER_REQUEST_GAP_SEC  = float(os.getenv("PER_REQUEST_GAP_SEC", 0.09))
PRICE_RETRIES        = int(os.getenv("PRICE_RETRIES", 3))

# TTL وتجديد
TTL_MIN              = int(os.getenv("TTL_MIN", 30))
SPREAD_MAX_BP        = int(os.getenv("SPREAD_MAX_BP", 60))
ALERT_COOLDOWN_SEC   = int(os.getenv("ALERT_COOLDOWN_SEC", 180))

# منطق تأكيد الحركة
WARMUP_SEC           = int(os.getenv("WARMUP_SEC", 3))
NUDGE_R20            = float(os.getenv("NUDGE_R20", 0.12))
NUDGE_R40            = float(os.getenv("NUDGE_R40", 0.20))
BREAKOUT_BP          = float(os.getenv("BREAKOUT_BP", 6.0))
DD60_MAX             = float(os.getenv("DD60_MAX", 0.25))
GLOBAL_ALERT_GAP     = int(os.getenv("GLOBAL_ALERT_GAP", 10))
CHASE_R5M_MAX        = float(os.getenv("CHASE_R5M_MAX", 2.20))
CHASE_R20_MIN        = float(os.getenv("CHASE_R20_MIN", 0.05))

# Telegram + Saqar
BOT_TOKEN            = os.getenv("BOT_TOKEN", "")
CHAT_ID              = os.getenv("CHAT_ID", "")
SAQAR_WEBHOOK        = os.getenv("SAQAR_WEBHOOK", "")

# Redis
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_MAX_SAMPLES    = int(os.getenv("REDIS_MAX_SAMPLES", 6000))
REDIS_TRIM_EVERY     = int(os.getenv("REDIS_TRIM_EVERY", 200))

rds = redis.from_url(REDIS_URL, decode_responses=True)

# =========================
# HTTP + كاش 24h
# =========================
session = requests.Session()
session.headers.update({"User-Agent":"TopN-Watcher/Redis"})
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

def http_get(path, params=None, base=BITVAVO_URL, timeout=HTTP_TIMEOUT):
    url = f"{base}{path}"
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(0.25 + 0.25*attempt); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 3:
                print(f"[HTTP] GET {path} failed:", e); return None
            time.sleep(0.15 + 0.15*attempt)
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
# أدوات
# =========================
def pct(a, b):
    if b is None or b == 0: return 0.0
    return (a - b) / b * 100.0

VALID_MKT = re.compile(r"^[A-Z0-9]{1,10}-EUR$")
def is_valid_market(m): return bool(VALID_MKT.match(m or ""))

# =========================
# Coin (in-memory state)
# =========================
class Coin:
    __slots__ = ("market","symbol","entered_at","expires_at","last_alert_at",
                 "cv","buf","last_price","entry_price","silent_until","price_fail",
                 "insert_count")
    def __init__(self, market, symbol, ttl_sec):
        t = time.time()
        self.market = market
        self.symbol = symbol
        self.entered_at = t
        self.expires_at = t + ttl_sec
        self.last_alert_at = 0.0
        self.cv = {}
        self.buf = deque(maxlen=2400)   # ~40 دقيقة على ~1s-2s
        self.last_price = None
        self.entry_price = None
        self.silent_until = t + WARMUP_SEC
        self.price_fail = 0
        self.insert_count = 0

    def r_change_local(self, seconds: int) -> float:
        if len(self.buf) < 2: return 0.0
        t_now, p_now = self.buf[-1]
        t_target = t_now - seconds
        base = None
        for (tt,pp) in reversed(self.buf):
            if tt <= t_target:
                base = pp; break
        if base is None: base = self.buf[0][1]
        return pct(p_now, base)

    def since_entry(self) -> float:
        if self.entry_price is None or self.last_price is None:
            return 0.0
        return pct(self.last_price, self.entry_price)

# =========================
# الغرفة
# =========================
room_lock = threading.Lock()
room = {}  # market -> Coin

def ensure_coin(cv):
    m   = (cv.get("market") or "").upper()
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
                if c.entry_price is None:
                    c.entry_price = p0
                _redis_append_price(m, nowt, p0, c)
            c.expires_at = nowt + TTL_MIN*60
            # لا تعيد ضبط silent_until هنا حتى لا يظل صامتاً
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
            c.last_price  = p0
            c.entry_price = p0
            c.buf.append((nowt, p0))
            _redis_append_price(m, nowt, p0, c)
        room[m] = c

# =========================
# Redis helpers
# =========================
def _rkey(market): return f"px:{market}"

def _redis_append_price(market, ts, price, c: Coin):
    try:
        rds.rpush(_rkey(market), f"{int(ts)}|{price:.12f}")
        c.insert_count += 1
        if c.insert_count % REDIS_TRIM_EVERY == 0:
            llen = rds.llen(_rkey(market))
            if llen and llen > REDIS_MAX_SAMPLES:
                rds.ltrim(_rkey(market), max(0, llen-REDIS_MAX_SAMPLES), -1)
            rds.expire(_rkey(market), 6*3600)
    except Exception as e:
        print("[REDIS] append failed:", e)

def _redis_last_price(market):
    try:
        v = rds.lindex(_rkey(market), -1)
        if not v: return None, None
        ts_s, pr_s = (v.split("|", 1) if "|" in v else (None, None))
        return (int(ts_s) if ts_s else None), (float(pr_s) if pr_s else None)
    except Exception:
        return None, None

def _redis_price_at(market, seconds_ago: int):
    try:
        now_s = int(time.time())
        target = now_s - int(seconds_ago)
        arr = rds.lrange(_rkey(market), -REDIS_MAX_SAMPLES, -1)
        for v in reversed(arr):
            if "|" not in v: continue
            ts_s, pr_s = v.split("|", 1)
            ts = int(ts_s); pr = float(pr_s)
            if ts <= target:
                return pr
        if arr:
            ts_s, pr_s = arr[0].split("|", 1)
            return float(pr_s)
    except Exception as e:
        print("[REDIS] price_at error:", e)
    return None

def r_change_redis(market, seconds_ago: int, last_price_now: float):
    base = _redis_price_at(market, seconds_ago)
    if base is None or last_price_now is None:
        return None  # <— تمييز عدم وجود قاعدة مقارنة
    return pct(last_price_now, base)

# لحظي: r5/r10 حي (Redis → local buffer كبديل)
def live_r_change(market, coin: Coin, seconds: int) -> float:
    val = r_change_redis(market, seconds, coin.last_price)
    if val is None:
        return coin.r_change_local(seconds)
    return val

# =========================
# جلب الأسعار (عنيد + بدائل)
# =========================
_last_candle_fetch = {}

def get_price_one(market):
    for _ in range(max(2, PRICE_RETRIES)):
        data = http_get("/v2/ticker/price", params={"market": market})
        try:
            if isinstance(data, dict):
                p = float(data.get("price") or 0)
            elif isinstance(data, list) and data:
                if len(data) == 1:
                    p = float((data[0] or {}).get("price") or 0)
                else:
                    row = next((x for x in data if x.get("market")==market), None)
                    p = float((row or {}).get("price") or 0)
            else:
                p = 0.0
        except Exception:
            p = 0.0
        if p > 0:
            return p
        time.sleep(0.08)

    book = http_get(f"/v2/{market}/book", params={"depth": 1})
    try:
        if isinstance(book, dict):
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            ask = float(asks[0][0]) if asks else 0.0
            bid = float(bids[0][0]) if bids else 0.0
            if ask > 0 and bid > 0:
                mid = (ask + bid)/2.0
                if mid > 0:
                    return mid
    except Exception:
        pass

    now = time.time()
    if now - _last_candle_fetch.get(market, 0) >= 10:
        cnd = http_get(f"/v2/{market}/candles", params={"interval":"1m", "limit": 1})
        _last_candle_fetch[market] = now
        try:
            if isinstance(cnd, list) and cnd:
                close = float(cnd[-1][4] or 0)
                t_ms  = float(cnd[-1][0] or 0)
                if close > 0 and (now*1000 - t_ms) <= 60_000:
                    return close
        except Exception:
            pass

    data24 = get_24h_cached(1.0)
    if data24:
        try:
            it = next((x for x in data24 if x.get("market")==market), None)
            last = float((it or {}).get("last", 0) or 0)
            if last > 0:
                return last
        except Exception:
            pass

    return None

def price_poller_loop():
    while True:
        start = time.time()
        with room_lock:
            markets = list(room.keys())
        if not markets:
            time.sleep(0.5); continue

        for m in markets:
            p = get_price_one(m)
            if not (p and p > 0):
                with room_lock:
                    c = room.get(m)
                    if c:
                        c.price_fail += 1
                        if c.price_fail % 5 == 0:
                            print(f"[PRICE] {m} failing ({c.price_fail}x)")
                time.sleep(PER_REQUEST_GAP_SEC); continue

            ts = time.time()
            with room_lock:
                c = room.get(m)
                if not c:
                    continue
                if c.price_fail >= 5:
                    print(f"[PRICE] {m} recovered after {c.price_fail} fails")
                c.price_fail = 0
                c.last_price = p
                c.buf.append((ts, p))
                if c.entry_price is None:
                    c.entry_price = p
                _redis_append_price(m, ts, p, c)

                if TTL_MIN > 0 and ts >= c.expires_at:
                    c.expires_at = ts + 120

            time.sleep(PER_REQUEST_GAP_SEC)

        elapsed = time.time() - start
        if SCAN_INTERVAL_SEC > elapsed:
            time.sleep(SCAN_INTERVAL_SEC - elapsed)

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

def recent_high_local(c: Coin, seconds: int):
    if not c.buf: return None
    t_now = c.buf[-1][0]
    vals = [p for (t,p) in c.buf if t >= t_now - seconds]
    return max(vals) if vals else None

def recent_dd_pct_local(c: Coin, seconds: int):
    if len(c.buf) < 2: return 0.0
    t_now = c.buf[-1][0]
    sub = [(t,p) for (t,p) in c.buf if t >= t_now - seconds]
    if not sub: return 0.0
    hi = max(p for _,p in sub); last = sub[-1][1]
    return (hi - last) / hi * 100.0

last_global_alert = 0.0
def decide_and_alert():
    global last_global_alert
    nowt = time.time()

    # ===== إعادة ترتيب لحظي حسب r5_live & r10_live =====
    with room_lock:
        scored = []
        for m, c in room.items():
            if c.last_price is None:
                continue
            r5  = live_r_change(m, c, 5*60)
            r10 = live_r_change(m, c, 10*60)
            score = r5 + 0.7*r10
            scored.append((score, r5, r10, m, c))
        scored.sort(reverse=True)  # الأعلى أولاً
        top_n = scored[:max(0, ALERT_TOP_N)]

    for score, r5_live, r10_live, m, c in top_n:
        if nowt < c.silent_until:
            continue
        if nowt - c.last_alert_at < ALERT_COOLDOWN_SEC:
            continue
        if nowt - last_global_alert < GLOBAL_ALERT_GAP:
            continue
        if c.last_price is None:
            continue

        # منع مطاردة: r5 حي مع شرط ربح قصير مقابل ركيزة 20s
        r20_loc = c.r_change_local(20)
        if r5_live >= CHASE_R5M_MAX and r20_loc < CHASE_R20_MIN:
            continue

        # نسب أطول من Redis (None -> 0.0)
        r20  = r_change_redis(m, 20*60,  c.last_price) or 0.0
        r60  = r_change_redis(m, 60*60,  c.last_price) or 0.0
        r120 = r_change_redis(m,120*60,  c.last_price) or 0.0

        r40_loc  = c.r_change_local(40)
        dd60_loc = recent_dd_pct_local(c, 60)
        hi60_loc = recent_high_local(c, 60)
        price_now = c.last_price
        if price_now is None or hi60_loc is None:
            continue

        breakout_ok = (price_now > hi60_loc * (1.0 + BREAKOUT_BP/10000.0))
        nudge_ok    = (r20 >= NUDGE_R20 and r40_loc >= NUDGE_R40)
        dd_ok       = (dd60_loc <= DD60_MAX)

        preburst = bool((c.cv or {}).get("preburst", False))
        if preburst:
            nudge_ok = (r20 >= max(0.08, NUDGE_R20-0.04) and r40_loc >= max(0.14, NUDGE_R40-0.06))
            breakout_ok = (price_now > hi60_loc * 1.0003)

        if not (nudge_ok and breakout_ok and dd_ok):
            continue
        if not spread_ok(m):
            continue

        c.last_alert_at = nowt
        last_global_alert = nowt
        saqar_buy(c.symbol)

# =========================
# الحلقات
# =========================
def monitor_loop():
    while True:
        try:
            decide_and_alert()
            if int(time.time()) % 30 == 0:
                with room_lock:
                    ok = sum(1 for c in room.values() if len(c.buf) >= 2 and c.last_price is not None)
                print(f"[MONITOR] buffers ok {ok}/{len(room)}")
        except Exception as e:
            print("[MONITOR] error:", e)
        time.sleep(TICK_SEC)

# =========================
# حالة وواجهات
# =========================
def build_status_text():
    with room_lock:
        rows = []
        scored = []
        nowt = time.time()
        for m, c in room.items():
            if c.last_price is None:
                continue
            r5  = live_r_change(m, c, 5*60)
            r10 = live_r_change(m, c, 10*60)
            score = r5 + 0.7*r10
            scored.append((score, r5, r10, m, c))

        scored.sort(reverse=True)
        for rank, (score, r5, r10, m, c) in enumerate(scored, start=1):
            r20  = r_change_redis(m, 20*60,  c.last_price) or 0.0
            r60  = r_change_redis(m, 60*60,  c.last_price) or 0.0
            r120 = r_change_redis(m,120*60,  c.last_price) or 0.0
            vz   = (c.cv or {}).get("volZ", 0.0)
            ttl  = int(c.expires_at - nowt)
            ttl_text = "∞" if TTL_MIN == 0 else f"{ttl}s"
            star = "⭐" if rank <= ALERT_TOP_N else " "
            since = c.since_entry()
            rows.append(
                f"{rank:02d}.{star} {m:<10} | r5 {r5:+.2f}%  r10 {r10:+.2f}%  "
                f"r20 {r20:+.2f}%  r60 {r60:+.2f}%  r120 {r120:+.2f}%  "
                f"SinceIn {since:+.2f}%  volZ {vz:+.2f}  Buf{len(c.buf)}  TTL {ttl_text}"
            )
    header = f"📊 Room {len(room)}/{ROOM_CAP} | TopN={ALERT_TOP_N} | Gap={GLOBAL_ALERT_GAP}s (sorted by r5 + 0.7*r10)"
    return header + ("\n" + "\n".join(rows) if rows else "\n(لا يوجد عملات بعد)")

app = Flask(__name__)

@app.route("/")
def root():
    return "TopN Watcher B is alive ✅"

@app.route("/status")
def status_http():
    return build_status_text(), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.route("/diag")
def diag():
    out = []
    now = time.time()
    with room_lock:
        for m,c in room.items():
            buflen = len(c.buf)
            last_ts, last_px = _redis_last_price(m)
            last_age = (now - last_ts) if last_ts else None
            out.append({
                "m": m, "buf_len": buflen,
                "redis_age_sec": round(last_age,2) if last_age is not None else None,
                "entry": c.entry_price, "last": c.last_price,
                "r5": round(live_r_change(m, c, 5*60),3),
                "r10": round(live_r_change(m, c,10*60),3),
                "r20": round((r_change_redis(m, 20*60,  c.last_price) or 0.0),3),
                "r60": round((r_change_redis(m, 60*60,  c.last_price) or 0.0),3),
                "r120":round((r_change_redis(m,120*60,  c.last_price) or 0.0),3),
            })
    return {"room": len(room), "items": out}, 200

@app.route("/ingest", methods=["POST"])
def ingest():
    cv = request.get_json(force=True, silent=True) or {}
    if not cv.get("market") or not cv.get("feat"):
        return jsonify(ok=False, err="bad payload"), 400
    ensure_coin(cv); return jsonify(ok=True)

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
        print("[WEBHOOK] err:", e); return jsonify(ok=True)

# =========================
# التشغيل — مسح Redis px:* مرة واحدة
# =========================
_boot_wiped = False
def wipe_redis_prices_once():
    global _boot_wiped
    if _boot_wiped: return
    try:
        cnt = 0
        for k in rds.scan_iter("px:*"):
            rds.delete(k); cnt += 1
        print(f"[REDIS] wiped price keys: {cnt}")
    except Exception as e:
        print("[REDIS] wipe error:", e)
    _boot_wiped = True

_threads_started = False
def start_threads():
    global _threads_started
    if _threads_started: return
    wipe_redis_prices_once()
    _threads_started = True
    threading.Thread(target=price_poller_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)