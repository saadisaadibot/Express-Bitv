# -*- coding: utf-8 -*-
"""
Bot B — TopN Watcher (Redis optional, Warmup + Nudge + Anti-Chase + Backoff429)
- يستقبل CV من A، يزرع السعر مباشرة في البافر، ويجدد TTL
- يراقب السعر الحيّ بمحاذير 429 (spacing + retries + backoff)
- يطلق تنبيه شراء لصقر فقط عند تحقق حركة حيّة مؤكدة (nudge + breakout + dd OK)
- يمنع المطاردة، ويطبّق فجوة عالمية بين التنبيهات
- يمسح Redis عند الإقلاع (إن وُجد)
- /status لعرض الغرفة بوضوح
"""

import os, time, math, json, threading, random
from collections import deque
from flask import Flask, request, jsonify
import requests

# =============== إعدادات ===============
BITVAVO_URL         = os.getenv("BITVAVO_URL", "https://api.bitvavo.com")
HTTP_TIMEOUT        = float(os.getenv("HTTP_TIMEOUT", 8.0))

ROOM_CAP            = int(os.getenv("ROOM_CAP", 24))
ALERT_TOP_N         = int(os.getenv("ALERT_TOP_N", 3))

# التوقيت
SCAN_INTERVAL_SEC   = float(os.getenv("SCAN_INTERVAL_SEC", 1.0))   # حلقة المراقبة
PER_REQUEST_GAP_SEC = float(os.getenv("PER_REQUEST_GAP_SEC", 0.08))# فاصل بين طلبات السعر
PRICE_RETRIES       = int(os.getenv("PRICE_RETRIES", 3))
WARMUP_SEC          = int(os.getenv("WARMUP_SEC", 20))             # بعد كل CV
BOOT_MUTE_SEC       = int(os.getenv("BOOT_MUTE_SEC", 25))          # بعد الإقلاع لتجنب Buy فوري

# قواعد القرار
R5M_MIN             = float(os.getenv("R5M_MIN", 0.80))            # أقل r5m لتبقى في TopN
NUDGE_R20           = float(os.getenv("NUDGE_R20", 0.12))
NUDGE_R40           = float(os.getenv("NUDGE_R40", 0.20))
BREAKOUT_BP         = float(os.getenv("BREAKOUT_BP", 6.0))         # اختراق قمة دقيقة (basis points)
DD60_MAX            = float(os.getenv("DD60_MAX", 0.25))
GLOBAL_ALERT_GAP    = int(os.getenv("GLOBAL_ALERT_GAP", 10))
ALERT_COOLDOWN_SEC  = int(os.getenv("ALERT_COOLDOWN_SEC", 150))
SPREAD_MAX_BP       = int(os.getenv("SPREAD_MAX_BP", 60))
NEG_SINCEIN_CUTOFF  = float(os.getenv("NEG_SINCEIN_CUTOFF", -2.0)) # لو القاعدة نزول قوي نسبياً

# TTL للرمز داخل الغرفة
TTL_MIN             = int(os.getenv("TTL_MIN", 30))                # دقيقة
STICKY_MIN          = int(os.getenv("STICKY_MIN", 5))

# Telegram / Saqar
BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
CHAT_ID             = os.getenv("CHAT_ID", "")
SAQAR_WEBHOOK       = os.getenv("SAQAR_WEBHOOK", "")

# Redis (اختياري)
REDIS_URL           = os.getenv("REDIS_URL", "").strip()

# =============== HTTP Session ===============
session = requests.Session()
session.headers.update({"User-Agent": "TopN-Watcher/4.1"})
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

def http_get(path, params=None, timeout=HTTP_TIMEOUT):
    url = f"{BITVAVO_URL}{path}"
    for i in range(PRICE_RETRIES):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                # backoff تصاعدي خفيف
                time.sleep(0.3 + i*0.3 + random.random()*0.2)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == PRICE_RETRIES-1:
                print(f"[HTTP] GET {path} failed:", e)
            time.sleep(0.15 + 0.1*i)
    return None

# =============== Redis (اختياري) ===============
rds = None
if REDIS_URL:
    try:
        import redis
        rds = redis.from_url(REDIS_URL)
        # مسح كل شيء عند الإقلاع
        try:
            n = 0
            for k in rds.scan_iter("*"):
                rds.delete(k); n += 1
            print(f"[REDIS] wiped {n} keys")
        except Exception as e:
            print("[REDIS] wipe error:", e)
    except Exception as e:
        print("[REDIS] connect error:", e)
        rds = None

# =============== كاش 24h خفيف ===============
_24h_cache = {"ts": 0.0, "data": []}
def get_24h_cached():
    now = time.time()
    # حدّث كل ~3 ثواني
    if now - _24h_cache["ts"] > 3.0:
        data = http_get("/v2/ticker/24h")
        if isinstance(data, list):
            _24h_cache["data"] = data
            _24h_cache["ts"] = now
    return _24h_cache["data"]

# =============== أدوات مساعدة ===============
def pct(a,b):
    try:
        if b is None or b == 0: return 0.0
        return (a-b)/b*100.0
    except: return 0.0

def tg_send(text):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=8)
    except Exception as e:
        print("[TG] send failed:", e)

def saqar_buy(symbol):
    url = (SAQAR_WEBHOOK or "").strip()
    if not url: return
    payload = {"text": f"اشتري {symbol.lower()}"}
    try:
        r = session.post(url, json=payload, timeout=8)
        if 200 <= r.status_code < 300:
            print(f"[SAQAR] ✅ اشتري {symbol.lower()}")
        else:
            print(f"[SAQAR] ❌ {r.status_code} {r.text[:160]}")
    except Exception as e:
        print("[SAQAR] error:", e)

# =============== حالة العملة ===============
class Coin:
    __slots__ = ("market","symbol","entered_at","expires_at","sticky_until",
                 "last_alert_at","cv","buf","last_price","armed_at","since_in")
    def __init__(self, market, symbol, ttl_sec):
        t = time.time()
        self.market = market
        self.symbol = symbol
        self.entered_at = t
        self.expires_at = t + ttl_sec
        self.sticky_until = t + STICKY_MIN*60
        self.last_alert_at = 0.0
        self.cv = {}                       # r5m, r10m, volZ, pct24, spread_bp, ...
        self.buf = deque(maxlen=1800)      # ~30 دقيقة على 1s
        self.last_price = None
        self.armed_at = t                  # للتسليح بعد CV/WARMUP
        self.since_in = None               # baseline وقت الدخول (للعرض)

    def r_change(self, seconds):
        if len(self.buf) < 2: return 0.0
        t_now, p_now = self.buf[-1]
        t_target = t_now - seconds
        base = None
        for (ts, pr) in reversed(self.buf):
            if ts <= t_target:
                base = pr; break
        if base is None: base = self.buf[0][1]
        return pct(p_now, base)

# =============== الغرفة ===============
room_lock = threading.Lock()
room = {}  # market -> Coin
boot_ts = time.time()
last_global_alert = 0.0

def ensure_coin(cv):
    """إدخال/تحديث عملة من A مع زرع السعر وتجديد المؤقتات."""
    m = cv["market"]
    sym = cv.get("symbol", m.split("-")[0])
    feat = cv.get("feat", {})
    nowt = time.time()
    ttl_sec = max(60, int(cv.get("ttl_sec", TTL_MIN*60)))

    with room_lock:
        c = room.get(m)
        if c:
            c.cv.update(feat)
            p0 = float(feat.get("price_now") or 0.0)
            if p0 > 0:
                c.last_price = p0
                c.buf.append((nowt, p0))
            # جدد 30 دقيقة + وارم-أب
            c.expires_at   = nowt + TTL_MIN*60
            c.armed_at     = nowt + WARMUP_SEC
            if c.since_in is None and p0 > 0:
                c.since_in = p0
            return

        # قص زائد لو ممتلئة الغرفة ولم تكن الجديدة أقوى
        if len(room) >= ROOM_CAP:
            # أضعف حسب r5m من CV
            weakest = min(room.items(), key=lambda kv: float((kv[1].cv or {}).get("r5m", 0.0)))
            if float(feat.get("r5m", 0.0)) <= float((weakest[1].cv or {}).get("r5m", 0.0)):
                return
            room.pop(weakest[0], None)

        c = Coin(m, sym, ttl_sec)
        c.cv.update(feat)
        p0 = float(feat.get("price_now") or 0.0)
        if p0 > 0:
            c.last_price = p0
            c.buf.append((nowt, p0))
            c.since_in = p0
        c.armed_at = nowt + WARMUP_SEC
        room[m] = c

# =============== السعر + السبريد ===============
def fetch_price(market):
    """يحاول price ثم fallback من 24h cache."""
    # 1) /ticker/price (أدق)
    data = http_get("/v2/ticker/price", params={"market": market})
    p = None
    try:
        if isinstance(data, dict):
            p = float(data.get("price") or 0.0)
        elif isinstance(data, list) and data:
            if len(data) == 1:
                p = float((data[0] or {}).get("price") or 0.0)
            else:
                row = next((x for x in data if x.get("market")==market), None)
                if row: p = float(row.get("price") or 0.0)
    except Exception:
        p = None

    # 2) fallback من 24h (كاش)
    if p is None or p <= 0:
        arr = get_24h_cached()
        if arr:
            it = next((x for x in arr if x.get("market")==market), None)
            try:
                p = float((it or {}).get("last", 0) or 0)
            except Exception:
                p = None
    return p

def spread_ok(market):
    arr = get_24h_cached()
    if not arr: return True
    it = next((x for x in arr if x.get("market")==market), None)
    if not it: return True
    try:
        bid = float(it.get("bid", 0) or 0); ask = float(it.get("ask", 0) or 0)
        if bid<=0 or ask<=0: return True
        bp = (ask - bid) / ((ask+bid)/2) * 10000.0
        return bp <= SPREAD_MAX_BP
    except: return True

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
    if hi <= 0: return 0.0
    return (hi - last) / hi * 100.0

# =============== القرار والتنبيه ===============
def decide_and_alert():
    global last_global_alert
    nowt = time.time()

    with room_lock:
        # رتّب حسب r5m من CV
        sorted_room = sorted(room.items(), key=lambda kv: float((kv[1].cv or {}).get("r5m", 0.0)), reverse=True)
        top_n = sorted_room[:max(0, ALERT_TOP_N)]

        for m, c in top_n:
            # شروط عامة
            if nowt - boot_ts < BOOT_MUTE_SEC:         # منع شراء عند الإقلاع
                continue
            if nowt < c.armed_at:                      # وارم-أب بعد CV
                continue
            if nowt - c.last_alert_at < ALERT_COOLDOWN_SEC:
                continue
            if nowt - last_global_alert < GLOBAL_ALERT_GAP:
                continue

            # لو r5m ضعيف جدًا لا نطارد
            r5m = float((c.cv or {}).get("r5m", 0.0))
            if r5m < R5M_MIN: 
                continue

            # المتغيرات اللحظية من البافر
            r20  = c.r_change(20)
            r40  = c.r_change(40)
            r60  = c.r_change(60)
            r120 = c.r_change(120)
            dd60 = recent_dd_pct(c, 60)
            price_now = c.last_price
            hi60 = recent_high(c, 60)
            if price_now is None or hi60 is None:
                continue

            # baseline عند الدخول
            if c.since_in is None:
                c.since_in = price_now
            since_in = pct(price_now, c.since_in)

            # anti-chase: لا تشتري لو القاعدة نزول قوي
            if since_in <= NEG_SINCEIN_CUTOFF:
                continue

            nudge_ok   = (r20 >= NUDGE_R20 and r40 >= NUDGE_R40)
            breakout_ok= (price_now > hi60 * (1.0 + BREAKOUT_BP/10000.0))
            dd_ok      = (dd60 <= DD60_MAX)
            if not (nudge_ok and breakout_ok and dd_ok):
                continue
            if not spread_ok(m):
                continue

            # ✅ Buy
            c.last_alert_at = nowt
            last_global_alert = nowt
            saqar_buy(c.symbol)

# =============== المراقبة ===============
def monitor_loop():
    """يجلب الأسعار حيًا مع spacing ضد 429؛ ويحدّث TTL/قص الغرفة."""
    rr = 0
    while True:
        t0 = time.time()
        try:
            with room_lock:
                markets = list(room.keys())
            n = len(markets)
            if n == 0:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # خذ subset صغير كل لفة لتقليل الضغط
            step = max(1, int(1.0/SCAN_INTERVAL_SEC))  # مجرد توزيع
            m = markets[rr:rr+step]
            if not m:
                rr = 0; 
                m = markets[:step]
            rr += step

            for mk in m:
                p = fetch_price(mk)
                if p is not None and p > 0:
                    ts = time.time()
                    with room_lock:
                        c = room.get(mk)
                        if not c: continue
                        c.last_price = p
                        c.buf.append((ts, p))
                        # انتهاء TTL؟
                        if ts >= c.expires_at:
                            # لا نقصي فورًا: نجدد قليلاً إن sticky أو قوية
                            if ts < c.sticky_until or float((c.cv or {}).get("r5m",0.0)) >= R5M_MIN:
                                c.expires_at = ts + 120
                            else:
                                room.pop(mk, None)
                time.sleep(PER_REQUEST_GAP_SEC)

            # قرار بعد جولة القراءة
            decide_and_alert()

        except Exception as e:
            print("[MONITOR] error:", e)
        # pacing عام
        spent = time.time() - t0
        time.sleep(max(0.0, SCAN_INTERVAL_SEC - spent))

# =============== Status ===============
def build_status_text():
    with room_lock:
        sorted_room = sorted(room.items(), key=lambda kv: float((kv[1].cv or {}).get("r5m", 0.0)), reverse=True)
        rows = []
        for rank, (m, c) in enumerate(sorted_room, start=1):
            r5m  = float((c.cv or {}).get("r5m", 0.0))
            r10m = float((c.cv or {}).get("r10m", 0.0))
            volZ = float((c.cv or {}).get("volZ", 0.0))
            r20  = c.r_change(20); r60 = c.r_change(60); r120 = c.r_change(120)
            ttl  = int(c.expires_at - time.time())
            ttl_txt = f"{ttl}s" if ttl>0 else "0s"
            since_in = 0.0
            try:
                since_in = pct((c.last_price or 0.0), c.since_in) if c.since_in else 0.0
            except: pass
            star = "⭐" if rank <= ALERT_TOP_N else "  "
            rows.append(f"{rank:02d}.{star} {m:<10} | r5m {r5m:+.2f}%  r10m {r10m:+.2f}%  "
                        f"r20 {r20:+.2f}%  r60 {r60:+.2f}%  r120 {r120:+.2f}%  "
                        f"SinceIn {since_in:+.2f}%  volZ {volZ:+.2f}  Buf{len(c.buf)}  TTL {ttl_txt}")
    hdr = f"📊 Room {len(room)}/{ROOM_CAP} | TopN={ALERT_TOP_N} | Gap={GLOBAL_ALERT_GAP}s"
    return hdr + ("\n" + "\n".join(rows) if rows else "\n(لا يوجد عملات)")

# =============== Flask API ===============
app = Flask(__name__)

@app.route("/")
def root():
    return "TopN Watcher B is alive ✅", 200

@app.route("/ingest", methods=["POST"])
def ingest():
    try:
        cv = request.get_json(force=True, silent=True) or {}
        if not cv.get("market") or not cv.get("feat"):
            return jsonify(ok=False, err="bad payload"), 400
        ensure_coin(cv)
        return jsonify(ok=True)
    except Exception as e:
        print("[INGEST] err:", e)
        return jsonify(ok=False), 200

@app.route("/status")
def status_http():
    return build_status_text(), 200, {"Content-Type":"text/plain; charset=utf-8"}

# =============== التشغيل ===============
def start_threads():
    threading.Thread(target=monitor_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)