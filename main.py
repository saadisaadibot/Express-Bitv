# -*- coding: utf-8 -*-
"""
Bot B — Lite
- Ingest من Bot A (Top10 items) أو عنصر مفرد قديم.
- Seeding للسلسلة [(ts, close)] داخل buffer لزيادة دقة r20/r60 فورًا.
- مراقبة سعر كل 2s فقط للأسواق تحت المراقبة (بدون Redis).
- لوج نسب r20/r40/r60/r180 و DD60 واضح.
- شرط Spark مبكّر (r20s & r60s + DD60 + spread) + كولداون وميزانية.
"""

import os, time, threading, re
from collections import deque
import requests
from flask import Flask, request, jsonify

# =========================
# إعدادات بسيطة (بدون .env)
# =========================
BITVAVO = "https://api.bitvavo.com/v2"
POLL_SEC = 2
WATCH_TTL_SEC = 6 * 60      # راقب كل رمز 6 دقائق بعد آخر إدخال
LOG_EVERY_SEC = 10
SPREAD_CHECK_EVERY_SEC = 30
GLOBAL_ALERT_GAP = 20       # ثانية بين إشارة وأخرى بشكل عام
ALERT_COOLDOWN_SEC = 120    # كولداون لكل رمز

# Spark (محافظ)
SPARK_R20_MIN = 0.6   # %
SPARK_R60_MIN = 0.6   # %
SPARK_DD60_MAX = 0.40 # %
SPARK_SPREAD_MAX_BP = 100  # 1.00%

# ميزانية إشارات بالدقيقة
MAX_FIRES_PER_MIN = 3

# Webhook للشراء (عدّل إذا لزم)
SAQR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/webhook"

# =========================
# Helpers
# =========================
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "BotB-Lite/1.0"})
adapter = requests.adapters.HTTPAdapter(max_retries=1, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

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
    if len(buf) < 2: return 0.0
    latest_t, latest_p = buf[-1]
    target = latest_t - sec
    ref = buf[0][1]
    for t, p in buf:
        if t >= target:
            ref = p; break
        ref = p
    return pct(latest_p, ref)

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

def post_saqar(sym):
    try:
        payload = {"text": f"اشتري {sym.lower()}"}
        r = session.post(SAQR_WEBHOOK, json=payload, timeout=8)
        ok = 200 <= r.status_code < 300
        print(f"[BUY]{'✅' if ok else '❌'} {sym} {r.status_code}")
    except Exception as e:
        print("[BUY] error:", e)

# =========================
# حالة داخلية
# =========================
# watch[mkt] = {
#   market, symbol, lastSeen, buf: deque[(ts,price)], lastSpreadTs, spreadBp,
#   cooldownUntil, lastLogTs
# }
watch = {}
fire_ts = deque(maxlen=20)  # طوابع الإطلاق لميزانية الدقيقة

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
    added = 0
    with lock:
        for it in items:
            m = it["market"]; s = it["symbol"]; last = it["last"]
            w = watch.get(m)
            if not w:
                w = watch[m] = {
                    "market": m, "symbol": s,
                    "lastSeen": ts,
                    "buf": deque(maxlen=3000),
                    "lastSpreadTs": 0.0, "spreadBp": None,
                    "cooldownUntil": 0.0, "lastLogTs": 0.0
                }
            else:
                w["lastSeen"] = ts
            if last > 0:
                w["buf"].append((ts, last))
            added += 1
    # زرع سلسلة A (آخر 3 دقائق)
    for it in items:
        if it.get("series"): seed_series(it["market"], it["series"])

    return jsonify(ok=True, added=added, watching=len(watch))

# =========================
# Polling + Logic
# =========================
def get_spread_bp(mkt):
    try:
        book = http_get(f"{BITVAVO}/{mkt}/book", params={"depth": 1})
        asks = book.get("asks") or []; bids = book.get("bids") or []
        ask = float(asks[0][0]) if asks else 0.0
        bid = float(bids[0][0]) if bids else 0.0
        if ask > 0 and bid > 0:
            mid = (ask + bid)/2.0
            return (ask - bid) / mid * 10000.0
    except Exception:
        return None
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

        # جلب كل الأسعار (bulk)
        try:
            allp = http_get(f"{BITVAVO}/ticker/price")
            price_map = {}
            if isinstance(allp, list):
                for it in allp:
                    price_map[it.get("market")] = float(it.get("price") or 0.0)
        except Exception as e:
            price_map = {}
            print("[poll] price error:", e)

        # حدّث كل سوق
        for m in mkts:
            with lock:
                w = watch.get(m)
            if not w: continue

            p = price_map.get(m, 0.0)
            if p and p > 0:
                with lock:
                    w["buf"].append((time.time(), p))
                    series_trim(w["buf"])

            # فحص السبريد دوريًا
            with lock:
                need_spread = (not w["lastSpreadTs"]) or (time.time() - w["lastSpreadTs"] >= SPREAD_CHECK_EVERY_SEC)
            if need_spread:
                bp = get_spread_bp(m)
                with lock:
                    w = watch.get(m)
                    if w:
                        w["lastSpreadTs"] = time.time()
                        if bp is not None: w["spreadBp"] = round(bp)

        # قرار + لوج
        decide_and_log()

        elapsed = time.time() - start
        if POLL_SEC > elapsed:
            time.sleep(POLL_SEC - elapsed)

def decide_and_log():
    nowt = time.time()
    # لوج دوري
    with lock:
        items = list(watch.items())
    for m, w in items:
        buf = w["buf"]
        if len(buf) < 2: continue
        r20 = r_change(buf, 20)
        r40 = r_change(buf, 40)
        r60 = r_change(buf, 60)
        r180 = r_change(buf, 180)
        dd = dd_max(buf, 60)
        if nowt - (w["lastLogTs"] or 0) >= LOG_EVERY_SEC:
            w["lastLogTs"] = nowt
            print(f"[R] {m:<10} r20 {r20:+.2f}%  r40 {r40:+.2f}%  r60 {r60:+.2f}%  r180 {r180:+.2f}%  DD60 {dd:+.2f}%  spread {w.get('spreadBp','-')}bp  pts {len(buf)}")

        # Spark مبكّر
        spread_ok = (w.get("spreadBp") is None) or (w["spreadBp"] <= SPARK_SPREAD_MAX_BP)
        can_fire_global = (not fire_ts) or (nowt - fire_ts[-1] >= GLOBAL_ALERT_GAP)
        if (r20 >= SPARK_R20_MIN and r60 >= SPARK_R60_MIN and dd <= SPARK_DD60_MAX
            and spread_ok and can_fire_global and nowt >= (w.get("cooldownUntil") or 0)):
            # ميزانية
            while fire_ts and nowt - fire_ts[0] > 60:
                fire_ts.popleft()
            if len(fire_ts) < MAX_FIRES_PER_MIN:
                fire_ts.append(nowt)
                post_saqar(w["symbol"])
                w["cooldownUntil"] = nowt + ALERT_COOLDOWN_SEC

# =========================
# واجهات فحص
# =========================
@app.get("/")
def root():
    return "Bot B Lite ✅", 200

@app.get("/status")
def status():
    with lock:
        syms = {m: {"pts": len(w["buf"]), "spreadBp": w["spreadBp"]} for m, w in watch.items()}
    return jsonify({"ok": True, "watching": len(watch), "symbols": syms})

@app.get("/preview")
def preview():
    out = {}
    with lock:
        for m, w in watch.items():
            buf = w["buf"]
            last = buf[-1][1] if buf else None
            out[m] = {"last": last, "points": len(buf), "spreadBp": w["spreadBp"]}
    return jsonify(out)

# =========================
# إقلاع
# =========================
def start_threads():
    threading.Thread(target=poll_prices_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)