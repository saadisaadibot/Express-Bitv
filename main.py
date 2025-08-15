# -*- coding: utf-8 -*-
"""
Bot B — Lite (Precision Mode)
- أسعار مباشرة من order book (mid) + Redis backfill
- Spark مع تأكيد/تسارع/اختراق + فلتر volZ + تبريد إعادة الإطلاق
- يرسل سبب الشراء إلى تيليغرام تلقائيًا
"""

import os, time, threading, re
from collections import deque
import requests, redis
from flask import Flask, request, jsonify

# =========================
# إعدادات يمكن تعديلها
# =========================
BITVAVO = "https://api.bitvavo.com/v2"
POLL_SEC = 2
WATCH_TTL_SEC = 6 * 60
SPREAD_CHECK_EVERY_SEC = 30
GLOBAL_ALERT_GAP = 20           # فاصل أدنى بين أي إشارتين
ALERT_COOLDOWN_SEC = 180        # كولداون لكل رمز بعد الإطلاق
REENTRY_COOLDOWN_SEC = 8 * 60   # عدم إعادة الإطلاق لنفس الرمز قبل أن يبرد

# شرارة + دقة
SPARK_R20_MIN     = 1.0         # كان 0.6
SPARK_R60_MIN     = 0.8         # كان 0.6
SPARK_DD60_MAX    = 0.30        # كان 0.40
SPARK_SPREAD_MAX_BP = 80        # كان 100

# تأكيد/تسارع/اختراق
CONFIRM_TICKS = 2               # لازم الشرط ينجح مرتين متتاليتين
CONFIRM_WINDOW_SEC = 6          # خلال هالنافذة
ACCEL_MIN_R40 = 0.15            # %
BREAKOUT_BP   = 18.0            # bp فوق أعلى سعر خلال 60s (مع استثناء آخر 2s)
MIN_VOLZ      = 0.20            # إن كانت متوفرة من A

MAX_FIRES_PER_MIN = 3

# ENV
SAQAR_WEBHOOK   = os.getenv("SAQAR_WEBHOOK", "")
TELEGRAM_TOKEN  = os.getenv("BOT_TOKEN", "")
CHAT_ID         = os.getenv("CHAT_ID", "")
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Redis
REDIS_MAX_SAMPLES = 6000
REDIS_EXPIRE_SEC  = 6 * 3600

# =========================
# تهيئة
# =========================
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "BotB-Lite/1.4"})
adapter = requests.adapters.HTTPAdapter(max_retries=1, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)
rds = redis.from_url(REDIS_URL, decode_responses=True)

def http_get(url, params=None, timeout=8.0):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def pct(a, b):
    if not b or b <= 0: return 0.0
    return (a - b) / b * 100.0

VALID_MKT = re.compile(r"^[A-Z0-9]{1,10}-EUR$")
def is_valid_market(m): return bool(VALID_MKT.match(m or ""))

def series_trim(buf, horizon=300):
    cutoff = time.time() - horizon - 2
    while buf and buf[0][0] < cutoff:
        buf.popleft()

def r_change(buf, sec):
    if len(buf) < 2: return 0.0
    latest_t, latest_p = buf[-1]
    target = latest_t - sec
    if buf[0][0] > target:
        return 0.0
    ref = None
    for t,p in buf:
        if t >= target:
            ref = p; break
        ref = p
    return pct(latest_p, ref or buf[0][1])

def dd_max(buf, sec=60):
    if len(buf) < 2: return 0.0
    latest_t = buf[-1][0]
    sub = [p for (t,p) in buf if t >= latest_t - sec]
    if not sub: return 0.0
    peak, worst = None, 0.0
    for p in sub:
        if peak is None or p > peak: peak = p
        if peak > 0:
            d = (p - peak) / peak * 100.0
            if d < worst: worst = d
    return abs(worst)

def recent_high_excl_last(buf, window_sec=60, excl_last_sec=2):
    if not buf: return None
    t_now = buf[-1][0]
    t_lo, t_hi = t_now - window_sec, t_now - excl_last_sec
    vals = [p for (t,p) in buf if t_lo <= t < t_hi]
    return max(vals) if vals else None

def base_symbol(m): return (m or "").upper().split("-")[0]

def send_message(text):
    if not (TELEGRAM_TOKEN and CHAT_ID): return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=8)
    except Exception:
        pass

def send_buy_reason(market, w, buf, r20, r60, dd, r40, bp_spread, brk_bp):
    if not (TELEGRAM_TOKEN and CHAT_ID): return
    last_price = buf[-1][1]
    ts_txt = time.strftime("%H:%M:%S", time.localtime(time.time()))
    vz = w.get("volZ", "-")
    text = (
        f"🧾 سبب شراء {market}:\n"
        f"💰 السعر {last_price:.6f}\n"
        f"r20 {r20:+.2f}% | r40 {r40:+.2f}% | r60 {r60:+.2f}% | DD60 {dd:+.2f}%\n"
        f"Breakout>{brk_bp:.1f}bp | Spread≈{(bp_spread if bp_spread is not None else '-') }bp | volZ {vz}\n"
        f"⏱ {ts_txt}"
    )
    send_message(text)

def post_saqar(sym):
    if not SAQAR_WEBHOOK: return
    try:
        payload = {"message": f"اشتري {sym.upper()}", "text": f"اشتري {sym.upper()}"}
        session.post(SAQAR_WEBHOOK, json=payload, timeout=8)
    except Exception:
        pass

# ========== Redis price helpers ==========
def _rkey(mkt): return f"px:{mkt}"

def redis_append_price(mkt, ts, price):
    try:
        rds.rpush(_rkey(mkt), f"{int(ts)}|{price:.12f}")
        llen = rds.llen(_rkey(mkt))
        if llen and llen > REDIS_MAX_SAMPLES:
            rds.ltrim(_rkey(mkt), -REDIS_MAX_SAMPLES, -1)
        rds.expire(_rkey(mkt), REDIS_EXPIRE_SEC)
    except Exception:
        pass

def redis_load_recent(mkt, seconds=300):
    try:
        now_s = int(time.time()); cutoff = now_s - int(seconds)
        arr = rds.lrange(_rkey(mkt), -REDIS_MAX_SAMPLES, -1)
        out = []
        for v in arr:
            if "|" not in v: continue
            ts_s, pr_s = v.split("|",1); ts = int(ts_s); pr = float(pr_s)
            if ts >= cutoff: out.append((float(ts), pr))
        return out
    except Exception:
        return []

def backfill_from_redis_if_needed(mkt, w, need_window_sec=60):
    buf = w["buf"]
    if not buf or buf[0][0] > (buf[-1][0] - need_window_sec):
        hist = redis_load_recent(mkt, seconds=300)
        if hist:
            merged = list(buf) + hist
            merged.sort(key=lambda x: x[0])
            # dedup by ts
            ded, last_ts = [], None
            for t,p in merged:
                if last_ts is None or t != last_ts:
                    ded.append((t,p)); last_ts = t
            w["buf"].clear(); w["buf"].extend(ded[-3000:])
            series_trim(w["buf"])

# =========================
# حالة داخلية
# =========================
# watch[m] = {..., 'confirm_hits': deque, 'last_fire_ts': 0}
watch = {}
fire_ts = deque(maxlen=20)
lock = threading.Lock()

# =========================
# Ingest
# =========================
def seed_series(mkt, series):
    if not series: return
    pts = []
    for row in series:
        try:
            t, p = int(row[0]), float(row[1])
            if p > 0: pts.append((float(t), p))
        except: pass
    if not pts: return
    pts.sort(key=lambda x: x[0])
    with lock:
        w = watch.get(mkt)
        if not w: return
        for (t,p) in pts:
            w["buf"].append((t,p))
            redis_append_price(mkt, t, p)
        series_trim(w["buf"])

def normalize_payload(body):
    items = []
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        for it in body["items"]:
            m = (it.get("market") or "").upper()
            if not is_valid_market(m): continue
            items.append({
                "market": m,
                "symbol": base_symbol(m),
                "last": float(it.get("last") or 0.0),
                "series": it.get("series") or [],
                "volZ": float(it.get("feat",{}).get("volZ", 0.0))
            })
        return items
    if isinstance(body, dict) and (body.get("market") or body.get("symbol")):
        m = (body.get("market") or "").upper()
        if is_valid_market(m):
            items.append({
                "market": m,
                "symbol": (body.get("symbol") or base_symbol(m)).upper(),
                "last": float(body.get("last") or body.get("feat",{}).get("price_now") or 0.0),
                "series": body.get("series") or [],
                "volZ": float(body.get("feat",{}).get("volZ", 0.0))
            })
        return items
    return []

@app.post("/ingest")
def ingest():
    body = request.get_json(force=True, silent=True) or {}
    items = normalize_payload(body)
    if not items:
        return jsonify(ok=False, err="bad payload"), 400

    ts = time.time()
    with lock:
        for it in items:
            m, s, last, vz = it["market"], it["symbol"], it["last"], it.get("volZ", 0.0)
            w = watch.get(m)
            if not w:
                w = watch[m] = {
                    "market": m, "symbol": s,
                    "lastSeen": ts,
                    "buf": deque(maxlen=3000),
                    "spreadBp": None,
                    "cooldownUntil": 0.0,
                    "lastSpreadTs": 0.0,
                    "confirm_hits": deque(maxlen=8),
                    "last_fire_ts": 0.0,
                    "volZ": vz
                }
                # backfill
                hist = redis_load_recent(m, seconds=300)
                for t,p in hist: w["buf"].append((t,p))
            else:
                w["lastSeen"] = ts
                w["volZ"] = vz if vz else w.get("volZ", 0.0)
            if last > 0:
                w["buf"].append((ts, last))
                redis_append_price(m, ts, last)

    for it in items:
        if it.get("series"): seed_series(it["market"], it["series"])
    return jsonify(ok=True, added=len(items), watching=len(watch))

# =========================
# LIVE price from book mid
# =========================
def get_mid_from_book(mkt):
    try:
        book = http_get(f"{BITVAVO}/{mkt}/book", params={"depth": 1}, timeout=6.0)
        asks = book.get("asks") or []; bids = book.get("bids") or []
        ask = float(asks[0][0]) if asks else 0.0
        bid = float(bids[0][0]) if bids else 0.0
        if ask > 0 and bid > 0:
            mid = (ask + bid)/2.0
            bp  = (ask - bid)/mid * 10000.0
            return mid, bp
    except Exception:
        return None, None
    return None, None

# =========================
# Polling
# =========================
def poll_prices_loop():
    while True:
        start = time.time()
        with lock:
            mkts = list(watch.keys())
        if not mkts:
            time.sleep(0.5); continue

        # تنظيف المنتهية
        nowt = time.time()
        with lock:
            for m in list(watch.keys()):
                if nowt - watch[m]["lastSeen"] > WATCH_TTL_SEC:
                    watch.pop(m, None)

        # تحديث الأسعار + السبريد
        for m in mkts:
            with lock:
                w = watch.get(m)
            if not w: continue

            price, bp = get_mid_from_book(m)
            if price and price > 0:
                ts = time.time()
                with lock:
                    w["buf"].append((ts, price))
                    series_trim(w["buf"])
                    if bp is not None:
                        w["spreadBp"] = round(bp)
                        w["lastSpreadTs"] = ts
                redis_append_price(m, ts, price)

        decide_loop()

        elapsed = time.time() - start
        if POLL_SEC > elapsed:
            time.sleep(POLL_SEC - elapsed)

# =========================
# قرار الشراء (مع تأكيد وتسارع واختراق)
# =========================
def decide_loop():
    nowt = time.time()
    with lock:
        items = list(watch.items())

    for m, w in items:
        buf = w["buf"]
        if len(buf) < 2:
            backfill_from_redis_if_needed(m, w, need_window_sec=60)
            buf = w["buf"]
            if len(buf) < 2: 
                continue

        latest_t = buf[-1][0]
        if buf[0][0] > (latest_t - 60):
            backfill_from_redis_if_needed(m, w, need_window_sec=60)
            buf = w["buf"]
            if buf[0][0] > (buf[-1][0] - 60):
                continue

        r20 = r_change(buf, 20)
        r40 = r_change(buf, 40)
        r60 = r_change(buf, 60)
        dd  = dd_max(buf, 60)
        hi_ex = recent_high_excl_last(buf, 60, 2)
        last = buf[-1][1]
        brk_bp = (last/hi_ex - 1.0) * 10000.0 if hi_ex else 0.0
        spread_ok = (w.get("spreadBp") is None) or (w["spreadBp"] <= SPARK_SPREAD_MAX_BP)

        # حواجز أساسية + volZ إن متوفر
        if not spread_ok: 
            continue
        if r20 < SPARK_R20_MIN or r60 < SPARK_R60_MIN or dd > SPARK_DD60_MAX:
            hit = False
        elif not (r20 >= r40 >= ACCEL_MIN_R40):
            hit = False
        elif not (hi_ex and brk_bp >= BREAKOUT_BP):
            hit = False
        elif w.get("volZ", 0.0) < MIN_VOLZ:
            hit = False
        else:
            hit = True

        # تأكيد: نحتاج hit مرتين خلال نافذة قصيرة
        with lock:
            q = w["confirm_hits"]
            now_i = time.time()
            q.append((now_i, 1 if hit else 0))
            # أبقِ فقط القيم ضمن النافذة
            while q and (now_i - q[0][0]) > CONFIRM_WINDOW_SEC:
                q.popleft()
            confirmed = sum(v for _,v in q) >= CONFIRM_TICKS

        # موانع الإطلاق
        if not fire_ts or (nowt - fire_ts[-1] >= GLOBAL_ALERT_GAP):
            global_gap_ok = True
        else:
            global_gap_ok = False

        if nowt < w.get("cooldownUntil", 0.0): 
            continue
        if (nowt - w.get("last_fire_ts", 0.0)) < REENTRY_COOLDOWN_SEC:
            continue

        if confirmed and global_gap_ok:
            # ميزانية/دقيقة
            while fire_ts and nowt - fire_ts[0] > 60:
                fire_ts.popleft()
            if len(fire_ts) < MAX_FIRES_PER_MIN:
                fire_ts.append(nowt)
                w["last_fire_ts"] = nowt
                w["cooldownUntil"] = nowt + ALERT_COOLDOWN_SEC
                # تنفيذ + إرسال سبب
                post_saqar(w["symbol"])
                bp_spread = w.get("spreadBp", None)
                send_buy_reason(m, w, buf, r20, r60, dd, r40, bp_spread, brk_bp)

# =========================
# أوامر تيليغرام
# =========================
_STATUS_ALIASES = {"الحالة","/الحالة","status","/status","شو عم تعمل","/شو_عم_تعمل"}

@app.post("/webhook")
def tg_webhook():
    data = request.get_json(force=True, silent=True) or {}
    msg  = data.get("message") or data.get("edited_message") or {}
    txt  = (msg.get("text") or "").strip()
    if txt and txt.lower() in _STATUS_ALIASES:
        send_status_report()
    return "ok"

def send_status_report():
    with lock:
        if not watch:
            send_message("📊 لا توجد عملات تحت المراقبة حاليًا."); return
        rows = []
        for m, w in watch.items():
            buf = w["buf"]
            if not buf: 
                backfill_from_redis_if_needed(m, w, need_window_sec=60)
                buf = w["buf"]
            if not buf: continue
            last = buf[-1][1]
            r20 = r_change(buf, 20)
            r60 = r_change(buf, 60)
            dd  = dd_max(buf, 60)
            spread = w.get("spreadBp", "-")
            rows.append((r20, f"{m}: 💰{last:.6f} | r20 {r20:+.2f}% | r60 {r60:+.2f}% | DD60 {dd:+.2f}% | spread {spread}bp"))
        rows.sort(reverse=True, key=lambda x: x[0])
        lines = ["📊 حالة المراقبة (مرتبة حسب r20):"] + [r[1] for r in rows]
    send_message("\n".join(lines))

# =========================
# HTTP
# =========================
@app.get("/")
def root():
    return "Bot B Lite — Precision ✅", 200

@app.get("/status")
def status_json():
    with lock:
        syms = {m: {"pts": len(w["buf"]), "spreadBp": w["spreadBp"], "volZ": w.get("volZ",0.0)} for m, w in watch.items()}
    return jsonify({"ok": True, "watching": len(watch), "symbols": syms})

# =========================
# تشغيل
# =========================
def start_threads():
    threading.Thread(target=poll_prices_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)