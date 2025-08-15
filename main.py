# -*- coding: utf-8 -*-
"""
Bot B — Lite + Telegram Status
"""

import os, time, threading, re
from collections import deque
import requests
from flask import Flask, request, jsonify

# =========================
# إعدادات
# =========================
BITVAVO = "https://api.bitvavo.com/v2"
POLL_SEC = 2
WATCH_TTL_SEC = 6 * 60
SPREAD_CHECK_EVERY_SEC = 30
GLOBAL_ALERT_GAP = 20
ALERT_COOLDOWN_SEC = 120

# Spark
SPARK_R20_MIN = 0.6
SPARK_R60_MIN = 0.6
SPARK_DD60_MAX = 0.40
SPARK_SPREAD_MAX_BP = 100
MAX_FIRES_PER_MIN = 3

SAQR_WEBHOOK = os.getenv("SAQR_WEBHOOK", "https://saadisaadibot-saqarxbo-production.up.railway.app/webhook")

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# =========================
# Helpers
# =========================
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "BotB-Lite/1.0"})

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
            d = (p - peak) / peak * 100.0
            if d < worst: worst = d
    return abs(worst)

def base_symbol(m): return (m or "").upper().split("-")[0]

def post_saqar(sym):
    try:
        payload = {"text": f"اشتري {sym.lower()}"}
        r = session.post(SAQR_WEBHOOK, json=payload, timeout=8)
        print(f"[BUY]{'✅' if r.ok else '❌'} {sym}")
    except Exception as e:
        print("[BUY] error:", e)

def send_message(text):
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            session.post(url, json={"chat_id": CHAT_ID, "text": text})
        except Exception as e:
            print("[TG] error:", e)

# =========================
# حالة داخلية
# =========================
watch = {}
fire_ts = deque(maxlen=20)
lock = threading.Lock()

# =========================
# Ingest + Seeding
# =========================
def seed_series(mkt, series):
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
    return []

@app.post("/ingest")
def ingest():
    body = request.get_json(force=True, silent=True) or {}
    items = normalize_payload(body)
    if not items:
        return jsonify(ok=False), 400
    ts = time.time()
    with lock:
        for it in items:
            m = it["market"]
            w = watch.get(m)
            if not w:
                w = watch[m] = {"market": m, "symbol": it["symbol"], "lastSeen": ts,
                                "buf": deque(maxlen=3000), "spreadBp": None,
                                "cooldownUntil": 0.0}
            w["lastSeen"] = ts
            if it["last"] > 0:
                w["buf"].append((ts, it["last"]))
    for it in items:
        if it.get("series"): seed_series(it["market"], it["series"])
    return jsonify(ok=True)

# =========================
# Polling
# =========================
def get_spread_bp(mkt):
    try:
        book = http_get(f"{BITVAVO}/{mkt}/book", params={"depth": 1})
        asks, bids = book.get("asks"), book.get("bids")
        ask = float(asks[0][0]) if asks else 0.0
        bid = float(bids[0][0]) if bids else 0.0
        if ask > 0 and bid > 0:
            mid = (ask + bid) / 2.0
            return (ask - bid) / mid * 10000.0
    except: return None

def poll_prices_loop():
    while True:
        start = time.time()
        with lock:
            mkts = list(watch.keys())
        nowt = time.time()
        with lock:
            for m in list(watch.keys()):
                if nowt - watch[m]["lastSeen"] > WATCH_TTL_SEC:
                    watch.pop(m, None)
        try:
            allp = http_get(f"{BITVAVO}/ticker/price")
            price_map = {it.get("market"): float(it.get("price") or 0.0) for it in allp}
        except: price_map = {}
        for m in mkts:
            with lock:
                w = watch.get(m)
            if not w: continue
            p = price_map.get(m)
            if p and p > 0:
                with lock:
                    w["buf"].append((time.time(), p))
                    series_trim(w["buf"])
            with lock:
                need_spread = (w.get("spreadBp") is None or time.time() - w.get("lastSpreadTs", 0) >= SPREAD_CHECK_EVERY_SEC)
            if need_spread:
                bp = get_spread_bp(m)
                with lock:
                    if w:
                        w["lastSpreadTs"] = time.time()
                        if bp is not None: w["spreadBp"] = round(bp)
        decide_and_log()
        elapsed = time.time() - start
        if POLL_SEC > elapsed:
            time.sleep(POLL_SEC - elapsed)

# =========================
# Logic
# =========================
def decide_and_log():
    nowt = time.time()
    with lock:
        items = list(watch.items())
    for m, w in items:
        buf = w["buf"]
        if len(buf) < 2: continue
        r20 = r_change(buf, 20)
        r60 = r_change(buf, 60)
        dd = dd_max(buf, 60)
        spread_ok = (w.get("spreadBp") is None) or (w["spreadBp"] <= SPARK_SPREAD_MAX_BP)
        can_fire_global = (not fire_ts) or (nowt - fire_ts[-1] >= GLOBAL_ALERT_GAP)
        if (r20 >= SPARK_R20_MIN and r60 >= SPARK_R60_MIN and dd <= SPARK_DD60_MAX
            and spread_ok and can_fire_global and nowt >= (w.get("cooldownUntil") or 0)):
            while fire_ts and nowt - fire_ts[0] > 60:
                fire_ts.popleft()
            if len(fire_ts) < MAX_FIRES_PER_MIN:
                fire_ts.append(nowt)
                post_saqar(w["symbol"])
                w["cooldownUntil"] = nowt + ALERT_COOLDOWN_SEC

# =========================
# أوامر تلغرام
# =========================
@app.post("/webhook")
def tg_webhook():
    data = request.get_json(force=True)
    text = (data.get("message", {}).get("text") or "").strip()
    if text.lower() in ["الحالة", "/status"]:
        send_status_report()
    return "ok"

def send_status_report():
    with lock:
        if not watch:
            send_message("📊 لا توجد عملات تحت المراقبة حالياً.")
            return
        lines = ["📊 حالة المراقبة:"]
        for m, w in watch.items():
            buf = w["buf"]
            if not buf: continue
            last_price = buf[-1][1]
            r20 = r_change(buf, 20)
            r60 = r_change(buf, 60)
            dd = dd_max(buf, 60)
            spread = w.get("spreadBp", "-")
            lines.append(f"{m}: 💰 {last_price:.6f} | r20 {r20:+.2f}% | r60 {r60:+.2f}% | DD60 {dd:+.2f}% | spread {spread}bp")
    send_message("\n".join(lines))

# =========================
# Run
# =========================
def start_threads():
    threading.Thread(target=poll_prices_loop, daemon=True).start()

start_threads()