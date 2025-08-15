# -*- coding: utf-8 -*-
"""
Bot B — Lite + Telegram Status + SAQAR webhook (ENV: SAQAR_WEBHOOK)
- يرسل تلقائياً سبب الشراء إلى تيليغرام مع كل إشارة شراء.
- يحفظ الأسعار في Redis ويقرأ مباشرةً لتعبئة نافذة r20/r60.
"""

import os, time, threading, re
from collections import deque
import requests, redis
from flask import Flask, request, jsonify

# =========================
# إعدادات
# =========================
BITVAVO = "https://api.bitvavo.com/v2"
POLL_SEC = 2                      # فترة جلب الأسعار
WATCH_TTL_SEC = 6 * 60            # TTL مراقبة الرمز بعد آخر ingest
SPREAD_CHECK_EVERY_SEC = 30       # كل كم ثانية نفحص السبريد
GLOBAL_ALERT_GAP = 20             # فاصل أدنى بين أي إشارتين
ALERT_COOLDOWN_SEC = 120          # كولداون للرمز بعد الإطلاق

# Spark (شرارة مبكرة)
SPARK_R20_MIN = 0.6               # % خلال 20 ثانية
SPARK_R60_MIN = 0.6               # % خلال 60 ثانية
SPARK_DD60_MAX = 0.40             # % السحب الأقصى خلال 60s
SPARK_SPREAD_MAX_BP = 100         # bp = 1.0%
MAX_FIRES_PER_MIN = 3             # حد أقصى إشارات/دقيقة

# Webhooks / Tokens (من ENV)
SAQAR_WEBHOOK   = os.getenv("SAQAR_WEBHOOK", "")
TELEGRAM_TOKEN  = os.getenv("BOT_TOKEN", "")
CHAT_ID         = os.getenv("CHAT_ID", "")
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Redis limits
REDIS_MAX_SAMPLES = 6000          # ~ عدة ساعات بدقة ثواني
REDIS_EXPIRE_SEC  = 6 * 3600      # احتفاظ 6 ساعات

# =========================
# Helpers
# =========================
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "BotB-Lite/1.2"})
adapter = requests.adapters.HTTPAdapter(max_retries=1, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

# Redis client
rds = redis.from_url(REDIS_URL, decode_responses=True)

def http_get(url, params=None, timeout=8.0):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def pct(new, old):
    if not old or old <= 0: return 0.0
    return (new - old) / old * 100.0

VALID_MKT = re.compile(r"^[A-Z0-9]{1,10}-EUR$")
def is_valid_market(m): return bool(VALID_MKT.match(m or ""))

def series_trim(buf, horizon=300):
    cutoff = time.time() - horizon - 2
    while buf and buf[0][0] < cutoff:
        buf.popleft()

def r_change(buf, sec):
    """نسبة التغير خلال نافذة sec: تستخدم أول نقطة >= target كمرجع.
       ترجع 0.0 إذا ما في مرجع أقدم من النافذة (منطقي لبداية التشغيل)."""
    if len(buf) < 2:
        return 0.0
    latest_t, latest_p = buf[-1]
    target = latest_t - sec
    if buf[0][0] > target:
        return 0.0
    ref_p = None
    for t, p in buf:
        if t >= target:
            ref_p = p
            break
    if ref_p is None:
        ref_p = buf[0][1]
    return pct(latest_p, ref_p)

def dd_max(buf, sec=60):
    if len(buf) < 2: return 0.0
    latest_t = buf[-1][0]
    sub = [p for (t,p) in buf if t >= latest_t - sec]
    if not sub: return 0.0
    peak, worst = None, 0.0
    for p in sub:
        if peak is None or p > peak: peak = p
        if peak > 0:
            d = (p - peak) / peak * 100.0  # negative
            if d < worst: worst = d
    return abs(worst)

def base_symbol(m): return (m or "").upper().split("-")[0]

def send_message(text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=8)
    except Exception:
        pass

def send_buy_reason(market: str, w: dict, buf: deque, r20: float, r60: float, dd: float):
    """يرسل شرح سبب الشراء لتلغرام فور الإطلاق."""
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    last_price = buf[-1][1]
    spread = w.get("spreadBp", "-")
    ts_txt = time.strftime("%H:%M:%S", time.localtime(time.time()))
    text = (
        f"🧾 سبب شراء {market}:\n"
        f"💰 السعر {last_price:.6f}\n"
        f"r20 {r20:+.2f}% | r60 {r60:+.2f}% | DD60 {dd:+.2f}%\n"
        f"spread {spread}bp | ⏱ {ts_txt}"
    )
    send_message(text)

def post_saqar(sym):
    if not SAQAR_WEBHOOK:
        print("[BUY]❌ SAQAR_WEBHOOK not set"); return
    try:
        payload = {"message": f"اشتري {sym.upper()}", "text": f"اشتري {sym.upper()}"}
        r = session.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        if 200 <= r.status_code < 300:
            print(f"[BUY]✅ {sym}")
        else:
            print(f"[BUY]❌ {sym} {r.status_code} body={r.text[:160]}")
    except Exception as e:
        print("[BUY] error:", e)

# ===== Redis price helpers =====
def _rkey(mkt): return f"px:{mkt}"

def redis_append_price(mkt: str, ts: float, price: float):
    """خزّن نقطة السعر بشكل list مع trimming و expire."""
    try:
        rds.rpush(_rkey(mkt), f"{int(ts)}|{price:.12f}")
        # تقليم
        llen = rds.llen(_rkey(mkt))
        if llen and llen > REDIS_MAX_SAMPLES:
            rds.ltrim(_rkey(mkt), -REDIS_MAX_SAMPLES, -1)
        # انتهاء صلاحية
        rds.expire(_rkey(mkt), REDIS_EXPIRE_SEC)
    except Exception:
        pass

def redis_load_recent(mkt: str, seconds: int = 300):
    """إرجاع [(ts,price)] من آخر seconds من Redis."""
    try:
        now_s = int(time.time())
        arr = rds.lrange(_rkey(mkt), -REDIS_MAX_SAMPLES, -1)
        out = []
        cutoff = now_s - int(seconds)
        for v in arr:
            if "|" not in v: continue
            ts_s, pr_s = v.split("|", 1)
            ts = int(ts_s); pr = float(pr_s)
            if ts >= cutoff:
                out.append((float(ts), pr))
        return out
    except Exception:
        return []

def backfill_from_redis_if_needed(mkt: str, w: dict, need_window_sec: int = 60):
    """لو الـ buf ما فيه نافذة كافية، عبّي من Redis آخر 5 دقائق."""
    try:
        buf = w["buf"]
        if not buf or buf[0][0] > (buf[-1][0] - need_window_sec):
            hist = redis_load_recent(mkt, seconds=300)
            if hist:
                # دمج مرتب بدون تكرار
                seen = {(int(t), float(p)) for (t,p) in buf}
                for t, p in hist:
                    key = (int(t), float(p))
                    if key not in seen:
                        buf.append((t, p))
                buf = deque(sorted(list(buf), key=lambda x: x[0]), maxlen=3000)
                w["buf"].clear(); w["buf"].extend(buf)
                series_trim(w["buf"])
    except Exception:
        pass

# =========================
# حالة داخلية
# =========================
# watch[mkt] = {market, symbol, lastSeen, buf, spreadBp, cooldownUntil, lastSpreadTs}
watch = {}
fire_ts = deque(maxlen=20)
lock = threading.Lock()

# =========================
# Ingest + Seeding
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
        for (t, p) in pts:
            w["buf"].append((t, p))
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
                "series": it.get("series") or []
            })
        return items
    if isinstance(body, dict) and (body.get("market") or body.get("symbol")):
        m = (body.get("market") or "").upper()
        if is_valid_market(m):
            items.append({
                "market": m,
                "symbol": (body.get("symbol") or base_symbol(m)).upper(),
                "last": float(body.get("last") or body.get("feat",{}).get("price_now") or 0.0),
                "series": body.get("series") or []
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
            m = it["market"]; s = it["symbol"]; last = it["last"]
            w = watch.get(m)
            if not w:
                w = watch[m] = {
                    "market": m, "symbol": s,
                    "lastSeen": ts,
                    "buf": deque(maxlen=3000),
                    "spreadBp": None,
                    "cooldownUntil": 0.0,
                    "lastSpreadTs": 0.0
                }
                # backfill فوري من Redis لو فيه تاريخ
                hist = redis_load_recent(m, seconds=300)
                if hist:
                    for t,p in hist:
                        w["buf"].append((t,p))
            else:
                w["lastSeen"] = ts
            if last > 0:
                w["buf"].append((ts, last))
                redis_append_price(m, ts, last)
    for it in items:
        if it.get("series"): seed_series(it["market"], it["series"])

    return jsonify(ok=True, added=len(items), watching=len(watch))

# =========================
# Polling
# =========================
def get_spread_bp(mkt):
    try:
        book = http_get(f"{BITVAVO}/{mkt}/book", params={"depth": 1})
        asks, bids = book.get("asks") or [], book.get("bids") or []
        ask = float(asks[0][0]) if asks else 0.0
        bid = float(bids[0][0]) if bids else 0.0
        if ask > 0 and bid > 0:
            mid = (ask + bid) / 2.0
            return (ask - bid) / mid * 10000.0
    except:
        return None

def poll_prices_loop():
    while True:
        start = time.time()
        with lock:
            mkts = list(watch.keys())
        if not mkts:
            time.sleep(0.5); continue

        # نظّف المنتهية
        nowt = time.time()
        with lock:
            for m in list(watch.keys()):
                if nowt - watch[m]["lastSeen"] > WATCH_TTL_SEC:
                    watch.pop(m, None)

        # أسعار bulk
        price_map = {}
        try:
            allp = http_get(f"{BITVAVO}/ticker/price")
            if isinstance(allp, list):
                for it in allp:
                    price_map[it.get("market")] = float(it.get("price") or 0.0)
        except:
            pass

        # تحديث نقاط + السبريد + حفظ Redis
        for m in mkts:
            with lock:
                w = watch.get(m)
            if not w: continue

            p = price_map.get(m, 0.0)
            if p and p > 0:
                ts = time.time()
                with lock:
                    w["buf"].append((ts, p))
                    series_trim(w["buf"])
                redis_append_price(m, ts, p)

            need_spread = False
            with lock:
                need_spread = (not w["lastSpreadTs"]) or (time.time() - w["lastSpreadTs"] >= SPREAD_CHECK_EVERY_SEC)
            if need_spread:
                bp = get_spread_bp(m)
                with lock:
                    w = watch.get(m)
                    if w:
                        w["lastSpreadTs"] = time.time()
                        if bp is not None: w["spreadBp"] = round(bp)

        decide_loop()

        elapsed = time.time() - start
        if POLL_SEC > elapsed:
            time.sleep(POLL_SEC - elapsed)

# =========================
# Logic (Spark فقط) + سبب الشراء لتلغرام
# =========================
def decide_loop():
    nowt = time.time()
    with lock:
        items = list(watch.items())
    for m, w in items:
        buf = w["buf"]
        if len(buf) < 2:
            # حاول نرجّع من Redis
            backfill_from_redis_if_needed(m, w, need_window_sec=60)
            buf = w["buf"]
            if len(buf) < 2:
                continue

        # 🔒 backfill إذا النافذة ناقصة
        latest_t = buf[-1][0]
        if buf[0][0] > (latest_t - 60):
            backfill_from_redis_if_needed(m, w, need_window_sec=60)
            buf = w["buf"]
            if buf[0][0] > (buf[-1][0] - 60):
                continue

        r20 = r_change(buf, 20)
        r60 = r_change(buf, 60)
        dd  = dd_max(buf, 60)
        spread_ok = (w.get("spreadBp") is None) or (w["spreadBp"] <= SPARK_SPREAD_MAX_BP)
        can_fire_global = (not fire_ts) or (nowt - fire_ts[-1] >= GLOBAL_ALERT_GAP)

        if (r20 >= SPARK_R20_MIN and r60 >= SPARK_R60_MIN and dd <= SPARK_DD60_MAX
            and spread_ok and can_fire_global and nowt >= (w.get("cooldownUntil") or 0)):
            # ميزانية الدقيقة
            while fire_ts and nowt - fire_ts[0] > 60:
                fire_ts.popleft()
            if len(fire_ts) < MAX_FIRES_PER_MIN:
                fire_ts.append(nowt)
                post_saqar(w["symbol"])                 # 1) شراء لصقر
                send_buy_reason(m, w, buf, r20, r60, dd) # 2) سبب الشراء تيليغرام
                w["cooldownUntil"] = nowt + ALERT_COOLDOWN_SEC

# =========================
# أوامر تلغرام
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
                # جرّب نسحب من Redis للعرض فقط
                backfill_from_redis_if_needed(m, w, need_window_sec=60)
                buf = w["buf"]
            if not buf:
                continue
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
# HTTP فحوص بسيطة
# =========================
@app.get("/")
def root():
    return "Bot B Lite ✅", 200

@app.get("/status")
def status_json():
    with lock:
        syms = {m: {"pts": len(w["buf"]), "spreadBp": w["spreadBp"]} for m, w in watch.items()}
    return jsonify({"ok": True, "watching": len(watch), "symbols": syms})

# =========================
# Run
# =========================
def start_threads():
    threading.Thread(target=poll_prices_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)