# -*- coding: utf-8 -*-
"""
Bot B — TopN Watcher (Adaptive) — refined
- score = r5_live + 0.7*r10_live
- r20 = 20s (قرار), r20m = 20m (عرض)
- اختراق 60s يستثني آخر 2s من النافذة
- لا تمديد TTL داخل price loop (prune دوري للمنتهية)
- spread من /book?depth=1 (مع كاش خفيف + fallback 24h)
- نفس دقة جلب السعر الحالية (لم تُمس)
"""

import os, time, threading, re
from collections import deque
import requests, redis
from flask import Flask, request, jsonify

# ============================================================
# 🔧 إعدادات عامة (قابلة للتعديل من هنا فقط)
# ============================================================
BITVAVO_URL          = "https://api.bitvavo.com"
HTTP_TIMEOUT         = 8.0
PER_REQUEST_GAP_SEC  = 0.09
PRICE_RETRIES        = 3

ROOM_CAP             = int(os.getenv("ROOM_CAP", "24"))
ALERT_TOP_N          = int(os.getenv("ALERT_TOP_N", "10"))
GLOBAL_ALERT_GAP     = float(os.getenv("GLOBAL_ALERT_GAP", "20"))  # ثوانٍ

TICK_SEC             = 1.0
SCAN_INTERVAL_SEC    = 2.0

TTL_MIN              = int(os.getenv("TTL_MIN", "30"))
SPREAD_MAX_BP        = float(os.getenv("SPREAD_MAX_BP", "70"))   # 1.0%
ALERT_COOLDOWN_SEC   = int(os.getenv("ALERT_COOLDOWN_SEC", "600"))

# مطاردة
CHASE_R5M_MAX        = float(os.getenv("CHASE_R5M_MAX", "5.0"))
CHASE_R20_MIN        = float(os.getenv("CHASE_R20_MIN", "0.02"))  # 20s

# ===== حواجز أمان أساسية (إذا عطّلت الديناميكي) =====
MIN_SCORE_BASE       = float(os.getenv("MIN_SCORE_BASE", "1.0"))  # r5 + 0.7*r10
MIN_R5_BASE          = float(os.getenv("MIN_R5_BASE", "0.6"))
MIN_R10_BASE         = float(os.getenv("MIN_R10_BASE", "0.4"))
MIN_VOLZ_BASE        = float(os.getenv("MIN_VOLZ_BASE", "0.0"))

# 🔁 ديناميكي حسب الوضع (فعّال افتراضياً)
USE_DYNAMIC_GUARDS   = os.getenv("USE_DYNAMIC_GUARDS", "1") not in ("0","false","False")
MIN_GUARDS = {
    # يمكنك تغيير الأرقام هنا أو عبر ENV بالأسفل
    "BULL":    {"score": float(os.getenv("BULL_SCORE", "0.8")),
                "r5":    float(os.getenv("BULL_R5",    "0.5")),
                "r10":   float(os.getenv("BULL_R10",   "0.3")),
                "volZ":  float(os.getenv("BULL_VOLZ",  "0.0"))},
    "NEUTRAL": {"score": float(os.getenv("NEUT_SCORE", "2.0")),
                "r5":    float(os.getenv("NEUT_R5",    "1.2")),
                "r10":   float(os.getenv("NEUT_R10",   "0.8")),
                "volZ":  float(os.getenv("NEUT_VOLZ",  "0.0"))},
    "BEAR":    {"score": float(os.getenv("BEAR_SCORE", "2.2")),
                "r5":    float(os.getenv("BEAR_R5",    "1.2")),
                "r10":   float(os.getenv("BEAR_R10",   "0.7")),
                "volZ":  float(os.getenv("BEAR_VOLZ",  "0.3"))},
}

# شرط تسارع
REQUIRE_ACCEL        = os.getenv("REQUIRE_ACCEL", "1") not in ("0","false","False")
ACCEL_MIN_R40        = float(os.getenv("ACCEL_MIN_R40", "0.15"))   # r40s يجب ألا يكون سالباً

# تشديد الـ preburst حسب الوضع
PREBURST_TUNING = {
    # زيادة على عتبات nudge و/أو اختراق بالـ bp
    "BULL":    {"nudge20_add": float(os.getenv("PB_BULL_N20_ADD", "-0.02")),
                "nudge40_add": float(os.getenv("PB_BULL_N40_ADD", "-0.03")),
                "breakout_bp": float(os.getenv("PB_BULL_BRK_BP",  "18.0"))},  # ~0.18%
    "NEUTRAL": {"nudge20_add": float(os.getenv("PB_NEUT_N20_ADD", "-0.02")),
                "nudge40_add": float(os.getenv("PB_NEUT_N40_ADD", "-0.03")),
                "breakout_bp": float(os.getenv("PB_NEUT_BRK_BP",  "20.0"))},
    "BEAR":    {"nudge20_add": float(os.getenv("PB_BEAR_N20_ADD", "0.10")),   # تشدد
                "nudge40_add": float(os.getenv("PB_BEAR_N40_ADD", "0.12")),
                "breakout_bp": float(os.getenv("PB_BEAR_BRK_BP",  "35.0"))},  # ~0.35%
}

# Fire-once + إعادة التسليح
FIRE_ONCE_PER_COIN   = os.getenv("FIRE_ONCE_PER_COIN", "1") not in ("0","false","False")
COOL_RESET_R20S      = float(os.getenv("COOL_RESET_R20S", "0.15"))

# تيليغرام عند الإطلاق
TELEGRAM_ON_FIRE     = os.getenv("TELEGRAM_ON_FIRE", "1") not in ("0","false","False")

# ميزانية تنبيهات بالدقيقة (للحد من الرش)
MAX_FIRES_PER_MIN    = int(os.getenv("MAX_FIRES_PER_MIN", "3"))

# عتبات أساسية (تُعدّل حسب الوضع) — الخاصة بإشارات nudge/breakout/dd
NUDGE_R20_BASE       = float(os.getenv("NUDGE_R20_BASE", "0.20"))
NUDGE_R40_BASE       = float(os.getenv("NUDGE_R40_BASE", "0.28"))
BREAKOUT_BP_BASE     = float(os.getenv("BREAKOUT_BP_BASE", "15.0"))  # bp
DD60_MAX_BASE        = float(os.getenv("DD60_MAX_BASE", "0.35"))

ADAPT_THRESHOLDS = {
    "BULL": {
        "NUDGE_R20": float(os.getenv("BULL_N20", "0.22")),
        "NUDGE_R40": float(os.getenv("BULL_N40", "0.32")),
        "BREAKOUT_BP": float(os.getenv("BULL_BRK", "18.0")),
        "DD60_MAX": float(os.getenv("BULL_DD60", "0.30")),
        "CHASE_R5M_MAX": float(os.getenv("BULL_CHASE_R5", "3.5")),
        "CHASE_R20_MIN": float(os.getenv("BULL_CHASE_R20", "0.05")),
    },
    "NEUTRAL": {
        "NUDGE_R20": NUDGE_R20_BASE, "NUDGE_R40": NUDGE_R40_BASE,
        "BREAKOUT_BP": BREAKOUT_BP_BASE, "DD60_MAX": DD60_MAX_BASE,
        "CHASE_R5M_MAX": CHASE_R5M_MAX, "CHASE_R20_MIN": CHASE_R20_MIN,
    },
    "BEAR": {
        "NUDGE_R20": float(os.getenv("BEAR_N20", "0.28")),
        "NUDGE_R40": float(os.getenv("BEAR_N40", "0.40")),
        "BREAKOUT_BP": float(os.getenv("BEAR_BRK", "25.0")),
        "DD60_MAX": float(os.getenv("BEAR_DD60", "0.25")),
        "CHASE_R5M_MAX": float(os.getenv("BEAR_CHASE_R5", "2.5")),
        "CHASE_R20_MIN": float(os.getenv("BEAR_CHASE_R20", "0.06")),
    },
}

# Redis
REDIS_MAX_SAMPLES    = 6000
REDIS_TRIM_EVERY     = 200

# .env
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
CHAT_ID       = os.getenv("CHAT_ID", "")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "")
REDIS_URL     = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ============================================================
# تهيئة
# ============================================================
rds = redis.from_url(REDIS_URL, decode_responses=True)

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

# -----------------------------
# أدوات
# -----------------------------
def pct(a, b):
    if b is None or b == 0: return 0.0
    return (a - b) / b * 100.0

VALID_MKT = re.compile(r"^[A-Z0-9]{1,10}-EUR$")
def is_valid_market(m): return bool(VALID_MKT.match(m or ""))

# -----------------------------
# Coin (in-memory state)
# -----------------------------
class Coin:
    __slots__ = ("market","symbol","entered_at","expires_at","last_alert_at",
                 "cv","buf","last_price","entry_price","silent_until","price_fail",
                 "insert_count")
    def __init__(self, market, symbol, ttl_sec):
        t = time.time()
        self.market = market
        self.symbol = symbol
        self.entered_at = t
        self.expires_at = t + ttl_sec if TTL_MIN > 0 else float("inf")
        self.last_alert_at = 0.0
        self.cv = {}
        self.buf = deque(maxlen=2400)   # ~40 دقيقة على ~1s-2s
        self.last_price = None
        self.entry_price = None
        self.silent_until = t + 1
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

# -----------------------------
# الغرفة
# -----------------------------
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
            # ⚠️ لا تمديد TTL هنا — يبقى ثابتًا منذ الإنشاء
            return

        if len(room) >= ROOM_CAP:
            # طرد أضعف عنصر بحسب score = r5m + 0.7*r10m من CV إن توفّر
            def _cv_score(cv_):
                return float(cv_.get("r5m", 0.0)) + 0.7*float(cv_.get("r10m", 0.0))
            weakest_mk, weakest_coin = min(room.items(), key=lambda kv: _cv_score(kv[1].cv))
            incoming_score = _cv_score(feat)
            if incoming_score <= _cv_score(weakest_coin.cv):
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

# -----------------------------
# Redis helpers
# -----------------------------
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
        return None
    return pct(last_price_now, base)

def live_r_change(market, coin: Coin, seconds: int) -> float:
    val = r_change_redis(market, seconds, coin.last_price)
    if val is None:
        return coin.r_change_local(seconds)
    return val

# -----------------------------
# جلب الأسعار (لا تغييرات هنا)
# -----------------------------
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

# -----------------------------
# spread من order book (+ كاش)
# -----------------------------
_book_cache = {}  # market -> {"ts": float, "bp": float}

def get_spread_bp_from_book(market, max_age=1.5):
    now = time.time()
    ent = _book_cache.get(market)
    if ent and (now - ent["ts"]) <= max_age:
        return ent["bp"]
    book = http_get(f"/v2/{market}/book", params={"depth": 1})
    try:
        if isinstance(book, dict):
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            ask = float(asks[0][0]) if asks else 0.0
            bid = float(bids[0][0]) if bids else 0.0
            if ask>0 and bid>0:
                mid = (ask+bid)/2.0
                bp = (ask - bid) / mid * 10000.0
                _book_cache[market] = {"ts": now, "bp": bp}
                return bp
    except Exception:
        pass
    return None

def spread_ok(market):
    bp = get_spread_bp_from_book(market)
    if bp is None:
        # fallback 24h
        data = get_24h_cached()
        if not data: return True
        it = next((x for x in data if x.get("market")==market), None)
        if not it: return True
        try:
            bid = float(it.get("bid", 0) or 0); ask = float(it.get("ask", 0) or 0)
            if bid<=0 or ask<=0: return True
            mid = (ask+bid)/2.0
            bp = (ask - bid) / mid * 10000.0
        except Exception:
            return True
    return bp <= SPREAD_MAX_BP

# -----------------------------
# أدوات قرار محلية
# -----------------------------
def recent_high_excl_last(c: "Coin", window_sec: int, exclude_last_sec: int = 2):
    """أعلى سعر ضمن نافذة window_sec لكن قبل آخر exclude_last_sec."""
    if not c.buf: return None
    t_now = c.buf[-1][0]
    t_lo  = t_now - window_sec
    t_hi  = t_now - max(0, exclude_last_sec)
    vals = [p for (t,p) in c.buf if (t_lo <= t < t_hi)]
    return max(vals) if vals else None

def recent_high_local(c: "Coin", seconds: int):
    if not c.buf: return None
    t_now = c.buf[-1][0]
    vals = [p for (t,p) in c.buf if t >= t_now - seconds]
    return max(vals) if vals else None

def recent_dd_pct_local(c: "Coin", seconds: int):
    if len(c.buf) < 2: return 0.0
    t_now = c.buf[-1][0]
    sub = [(t,p) for (t,p) in c.buf if t >= t_now - seconds]
    if not sub: return 0.0
    hi = max(p for _,p in sub); last = sub[-1][1]
    return (hi - last) / hi * 100.0

# -----------------------------
# ذكاء وضع السوق
# -----------------------------
def market_mode_snapshot():
    with room_lock:
        rows = []
        for m, c in room.items():
            if c.last_price is None:
                continue
            r60 = r_change_redis(m, 60*60, c.last_price)
            if r60 is None: r60 = c.r_change_local(60*60)
            r5abs = abs(live_r_change(m, c, 5*60))
            volZ = (c.cv or {}).get("volZ", 0.0)
            rows.append((r60, r5abs, volZ))

    if not rows:
        return "NEUTRAL", {"heat":0.0, "breadth":0.0, "vol":0.0}

    weights = [max(0.0, z + 1.0) for (_,_,z) in rows]
    r60s    = [r for (r,_,_) in rows]
    heat = sum(w*r for w, r in zip(weights, r60s)) / max(1e-9, sum(weights))
    breadth = sum(1 for r in r60s if r > 0) / len(r60s)
    vol = sum(a for (_,a,_) in rows) / len(rows)

    if heat > 0.30 and breadth > 0.60:
        mode = "BULL"
    elif heat < -0.30 and breadth < 0.40:
        mode = "BEAR"
    else:
        mode = "NEUTRAL"

    return mode, {"heat": heat, "breadth": breadth, "vol": vol}

# -----------------------------
# القرار + الإشعار
# -----------------------------
last_global_alert = 0.0
fired_symbols = {}  # market -> {"armed": True/False, "last_fire": ts}
_fire_ts = deque(maxlen=20)  # ميزانية إطلاق بالدقيقة

def _format_fire_msg(sym, mode, rank, score, r5, r10, r20s, r40s, dd60, bp_spread, hi60ex_bp):
    parts = [
        f"🟢 اشترِ {sym}",
        f"Mode: {mode} | Rank: #{rank} | Score: {score:+.2f}%",
        f"r5 {r5:+.2f}%  r10 {r10:+.2f}%  r20s {r20s:+.2f}%  r40s {r40s:+.2f}%",
        f"DD60 {dd60:+.2f}%  Breakout>{hi60ex_bp:.1f}bp  Spread≈{bp_spread:.0f}bp"
    ]
    return " | ".join(parts)

def _min_guards_for_mode(mode: str):
    if USE_DYNAMIC_GUARDS:
        g = MIN_GUARDS.get(mode, MIN_GUARDS["NEUTRAL"])
        return g["score"], g["r5"], g["r10"], g["volZ"]
    # static fallback
    return MIN_SCORE_BASE, MIN_R5_BASE, MIN_R10_BASE, MIN_VOLZ_BASE

def decide_and_alert():
    global last_global_alert
    nowt = time.time()

    # ترتيب لحظي + خريطة rank للعرض
    with room_lock:
        scored = []
        for m, c in room.items():
            if c.last_price is None:
                continue
            r5  = live_r_change(m, c, 5*60)
            r10 = live_r_change(m, c,10*60)
            score = r5 + 0.7*r10
            scored.append((score, r5, r10, m, c))
        scored.sort(reverse=True)
        top_n = scored[:max(0, ALERT_TOP_N)]
        ranks = {m: i+1 for i, (_,_,_,m,_) in enumerate(scored)}  # rank لكل سوق

    # وضع السوق + عتباته
    mode, mm = market_mode_snapshot()
    th = ADAPT_THRESHOLDS.get(mode, ADAPT_THRESHOLDS["NEUTRAL"])
    dyn_NUDGE_R20     = th["NUDGE_R20"]
    dyn_NUDGE_R40     = th["NUDGE_R40"]
    dyn_BREAKOUT_BP   = th["BREAKOUT_BP"]
    dyn_DD60_MAX      = th["DD60_MAX"]
    dyn_CHASE_R5M_MAX = th["CHASE_R5M_MAX"]
    dyn_CHASE_R20_MIN = th["CHASE_R20_MIN"]

    # حواجز دنيا (ديناميكي/ثابت)
    min_score, min_r5, min_r10, min_volz = _min_guards_for_mode(mode)

    for score, r5_live, r10_live, m, c in top_n:
        if nowt < c.silent_until:
            continue
        if c.last_price is None:
            continue

        # كولداون عالمي
        if nowt - last_global_alert < GLOBAL_ALERT_GAP:
            continue

        # حواجز دنيا على الزخم اللحظي
        if (score < min_score) or (r5_live < min_r5) or (r10_live < min_r10):
            continue

        # فلتر فوليوم من CV المُرسل من A
        vz = float((c.cv or {}).get("volZ", 0.0))
        if vz < min_volz:
            continue

        # r20s للمطاردة + تسارع
        r20s = live_r_change(m, c, 20) or 0.0

        # منع المطاردة: r5 عالي بدون زخم 20s
        if r5_live >= dyn_CHASE_R5M_MAX and r20s < dyn_CHASE_R20_MIN:
            continue

        # fire-once لكل رمز حتى يبرد
        st = fired_symbols.get(m)
        if FIRE_ONCE_PER_COIN:
            if st is None:
                fired_symbols[m] = {"armed": True, "last_fire": 0.0}
            else:
                if not st.get("armed", True):
                    # لازم يبرد: r20s تحت العتبة
                    if r20s >= COOL_RESET_R20S:
                        continue
                    else:
                        st["armed"] = True  # أعِد التسليح بعد البرود

        # إشارات قصيرة للقرار
        r40s   = c.r_change_local(40) or 0.0
        dd60   = recent_dd_pct_local(c, 60)
        hi60ex = recent_high_excl_last(c, 60, exclude_last_sec=2)
        price_now = c.last_price
        if price_now is None or hi60ex is None:
            continue

        # شرط تسارع اختياري
        if REQUIRE_ACCEL:
            if not (r20s >= r40s and r40s >= ACCEL_MIN_R40):
                continue

        # اختراق ونَجدة
        breakout_ok = (price_now > hi60ex * (1.0 + dyn_BREAKOUT_BP/10000.0))
        nudge_ok    = (r20s >= dyn_NUDGE_R20 and r40s >= dyn_NUDGE_R40)
        dd_ok       = (dd60 <= dyn_DD60_MAX)

        # تشديد/تخفيف preburst حسب الوضع
        preburst = bool((c.cv or {}).get("preburst", False))
        if preburst:
            tune = PREBURST_TUNING.get(mode, PREBURST_TUNING["NEUTRAL"])
            nudge_ok    = (r20s >= (dyn_NUDGE_R20 + tune["nudge20_add"]) and
                           r40s >= (dyn_NUDGE_R40 + tune["nudge40_add"]))
            req_bp = max(dyn_BREAKOUT_BP, tune["breakout_bp"])
            breakout_ok = (price_now > hi60ex * (1.0 + req_bp/10000.0))

        if not (nudge_ok and breakout_ok and dd_ok):
            continue
        if not spread_ok(m):
            continue

        # ميزانية: حد أقصى بالدقيقة
        while _fire_ts and nowt - _fire_ts[0] > 60:
            _fire_ts.popleft()
        if len(_fire_ts) >= MAX_FIRES_PER_MIN:
            continue

        # تفاصيل للعرض
        try:
            bp_spread = get_spread_bp_from_book(m) or 0.0
        except Exception:
            bp_spread = 0.0
        hi60ex_bp = (price_now/hi60ex - 1.0) * 10000.0 if hi60ex else 0.0
        rank = ranks.get(m, 0)
        sym = c.symbol

        # ✅ إطلاق الشراء
        c.last_alert_at = nowt
        last_global_alert = nowt
        saqar_buy(sym)
        _fire_ts.append(nowt)

        # رسالة تيليغرام مفصّلة
        if TELEGRAM_ON_FIRE:
            msg = _format_fire_msg(
                sym=sym, mode=mode, rank=rank, score=score,
                r5=r5_live, r10=r10_live, r20s=r20s, r40s=r40s,
                dd60=dd60, bp_spread=bp_spread, hi60ex_bp=hi60ex_bp
            )
            tg_send(msg)

        # عطّل الرمز لحد ما يبرد (fire-once)
        if FIRE_ONCE_PER_COIN:
            fired_symbols[m] = {"armed": False, "last_fire": nowt}

# -----------------------------
# الحلقات (مع prune للـ TTL)
# -----------------------------
def prune_expired():
    if TTL_MIN <= 0: return
    nowt = time.time()
    removed = []
    with room_lock:
        for m, c in list(room.items()):
            if nowt >= c.expires_at:
                room.pop(m, None); removed.append(m)
    if removed:
        print(f"[PRUNE] removed expired: {', '.join(removed)}")

def monitor_loop():
    while True:
        try:
            prune_expired()
            decide_and_alert()
            if int(time.time()) % 30 == 0:
                with room_lock:
                    ok = sum(1 for c in room.values() if len(c.buf) >= 2 and c.last_price is not None)
                print(f"[MONITOR] buffers ok {ok}/{len(room)}")
        except Exception as e:
            print("[MONITOR] error:", e)
        time.sleep(TICK_SEC)

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
                # ❌ لا تمديد TTL هنا — صار عبر ingest فقط

            time.sleep(PER_REQUEST_GAP_SEC)

        elapsed = time.time() - start
        if SCAN_INTERVAL_SEC > elapsed:
            time.sleep(SCAN_INTERVAL_SEC - elapsed)

# -----------------------------
# حالة وواجهات
# -----------------------------
def _mins(x):
    try: return max(0, int(x // 60))
    except Exception: return 0

def build_status_text():
    mode, mm = market_mode_snapshot()
    heat = mm["heat"]; breadth = mm["breadth"]
    mode_line = f"📊 Mode: {mode} | Heat: {heat:+.2f}% | Breadth: {int(breadth*100)}%"

    with room_lock:
        rows = []
        scored = []
        nowt = time.time()
        for m, c in room.items():
            if c.last_price is None:
                continue
            r5  = live_r_change(m, c, 5*60)
            r10 = live_r_change(m, c,10*60)
            score = r5 + 0.7*r10
            scored.append((score, r5, r10, m, c))

        scored.sort(reverse=True)
        for rank, (score, r5, r10, m, c) in enumerate(scored, start=1):
            r20s = live_r_change(m, c, 20) or 0.0
            r20m = r_change_redis(m, 20*60, c.last_price) or 0.0  # للعرض فقط
            r60  = r_change_redis(m, 60*60, c.last_price) or 0.0
            vz   = (c.cv or {}).get("volZ", 0.0)

            ttl_sec = (c.expires_at - nowt) if TTL_MIN > 0 else float("inf")
            ttl_txt = "∞" if TTL_MIN == 0 else f"{_mins(ttl_sec)}م"
            buf_min = _mins(nowt - c.entered_at)
            star = "⭐" if rank <= ALERT_TOP_N else " "
            since = c.since_entry()
            sym = m.split("-")[0]
            rows.append(
                f"{rank:02d}.{star} {sym:<6} | r5 {r5:+.2f}%  r10 {r10:+.2f}%  "
                f"r20s {r20s:+.2f}%  r20m {r20m:+.2f}%  r60 {r60:+.2f}%  "
                f"منذ دخول {since:+.2f}%  volZ {vz:+.2f}  🕒{buf_min}م  ⏳{ttl_txt}"
            )

    header = f"Room {len(room)}/{ROOM_CAP} | TopN={ALERT_TOP_N} | Gap={GLOBAL_ALERT_GAP}s (r5 + 0.7*r10)"
    body = "\n".join(rows) if rows else "(لا يوجد عملات بعد)"
    return f"{mode_line}\n{header}\n{body}"

# -----------------------------
# Flask
# -----------------------------
app = Flask(__name__)

@app.route("/")
def root():
    return "TopN Watcher B (Adaptive) — refined ✅"

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
                "r20s": round(live_r_change(m, c, 20) or 0.0, 3),
                "r20m": round((r_change_redis(m, 20*60,  c.last_price) or 0.0),3),
                "r60":  round((r_change_redis(m, 60*60,  c.last_price) or 0.0),3),
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

# -----------------------------
# التشغيل — مسح Redis px:* مرة واحدة
# -----------------------------
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