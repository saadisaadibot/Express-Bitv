# -*- coding: utf-8 -*-
"""
Bot B — TopN Winner Hunter (Redis wipe + Smart Alerts)
- يمسح Redis عند الإقلاع
- ينسخ CV من A بدون تصفير، ويجدد TTL كل استلام
- ترتيب داخلي: score = r5m + 0.7*r10m (صيد الانفجار الحقيقي)
- استبعاد فوري إذا SinceIn ≤ NEG_SINCEIN_CUTOFF
- مراقبة سعر كل 1s، حساب r20/r60/r120 حيّ
- إشعارات لصقر "اشتري {symbol}" لأفضل ALERT_TOP_N فقط
- /status نصّي واضح
"""

import os, time, math, threading, random
from collections import deque
from flask import Flask, request, jsonify
import requests

# ======== إعدادات عامة ========
BITVAVO_URL         = os.getenv("BITVAVO_URL", "https://api.bitvavo.com")
HTTP_TIMEOUT        = float(os.getenv("HTTP_TIMEOUT", 8.0))

ROOM_CAP            = int(os.getenv("ROOM_CAP", 24))
TTL_MIN             = int(os.getenv("TTL_MIN", 30))            # دقائق
SCAN_SEC            = float(os.getenv("SCAN_SEC", 1.0))         # مراقبة الأسعار
BATCH_SIZE          = int(os.getenv("BATCH_SIZE", 16))          # لكل دورة

ALERT_TOP_N         = int(os.getenv("ALERT_TOP_N", 3))
ALERT_COOLDOWN_SEC  = int(os.getenv("ALERT_COOLDOWN_SEC", 150))
GLOBAL_ALERT_GAP    = int(os.getenv("GLOBAL_ALERT_GAP", 10))
SPREAD_MAX_BP       = int(os.getenv("SPREAD_MAX_BP", 40))       # 0.40%

# شروط بسيطة للإطلاق
NUDGE_R20           = float(os.getenv("NUDGE_R20", 0.12))
NUDGE_R40           = float(os.getenv("NUDGE_R40", 0.18))
BREAKOUT_BP         = float(os.getenv("BREAKOUT_BP", 8.0))      # اختراق قمة دقيقة (0.08%)
DD60_MAX            = float(os.getenv("DD60_MAX", 0.35))        # هبوط من قمة آخر دقيقة
R5M_MIN             = float(os.getenv("R5M_MIN", 0.80))         # حد أدنى منطقي
NEG_SINCEIN_CUTOFF  = float(os.getenv("NEG_SINCEIN_CUTOFF", -2.0))  # استبعاد الصفقات الخسرانة بقوة

# Telegram + Saqar
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
CHAT_ID       = os.getenv("CHAT_ID", "")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "")

# Redis (اختياري)
import redis
REDIS_URL = os.getenv("REDIS_URL", "")
rds = redis.from_url(REDIS_URL) if REDIS_URL else None

# ======== HTTP Session ========
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)
session.headers.update({"User-Agent": "WinnerHunter/1.0"})

def http_get(path, params=None, base=BITVAVO_URL, timeout=HTTP_TIMEOUT):
    url = f"{base}{path}"
    try:
        r = session.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            time.sleep(0.6 + random.random()*0.6)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[HTTP] GET {path} failed:", e)
        return None

# ======== أدوات ========
def pct(a, b):
    if b is None or b == 0: return 0.0
    return (a - b) / b * 100.0

def now(): return time.time()

# ======== إشعارات ========
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
    payload = {"text": f"اشتري {symbol.lower()}"}
    try:
        r = session.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        if 200 <= r.status_code < 300:
            print(f"[SAQAR] ✅ اشتري {symbol.lower()}")
        else:
            print(f"[SAQAR] ❌ {r.status_code} {r.text[:140]}")
    except Exception as e:
        print("[SAQAR] error:", e)

# ======== Coin State ========
class Coin:
    __slots__ = ("market","symbol","entered_at","expires_at","last_alert_at",
                 "cv","buf","last_price","entry_price","silent_until")
    def __init__(self, market, symbol, ttl_sec):
        t = now()
        self.market = market
        self.symbol = symbol
        self.entered_at = t
        self.expires_at = t + ttl_sec
        self.last_alert_at = 0
        self.cv = {}  # r5m/r10m/volZ/...
        self.buf = deque(maxlen=int(max(600, 1200/max(0.2, SCAN_SEC))))  # ~10–20 دقيقة
        self.last_price = None
        self.entry_price = None  # baseline لحساب SinceIn
        self.silent_until = t + 5  # إحماء قصير

    def r_change(self, seconds):
        if len(self.buf) < 2: return 0.0
        t_now, p_now = self.buf[-1]
        t_target = t_now - seconds
        base = None
        for (t,p) in reversed(self.buf):
            if t <= t_target:
                base = p; break
        if base is None: base = self.buf[0][1]
        return pct(p_now, base)

    def since_in(self):
        if self.last_price and self.entry_price and self.entry_price>0:
            return pct(self.last_price, self.entry_price)
        return 0.0

# ======== الغرفة ========
room_lock = threading.Lock()
room = {}  # market -> Coin
last_global_alert = 0.0

def ensure_coin(cv):
    """إدخال/تحديث من A. تجديد TTL دائماً. زرع price_now إن وُجد."""
    m   = cv["market"]
    sym = cv.get("symbol", m.split("-")[0])
    feat= cv.get("feat", {})
    ttl_sec = max(60, int(cv.get("ttl_sec", TTL_MIN*60)))
    nowt = now()

    with room_lock:
        c = room.get(m)
        if not c:
            # قص إذا ممتلئة: نحذف أضعف score (r5m+0.7*r10m)
            if len(room) >= ROOM_CAP:
                def score_of(cc):
                    f = cc.cv or {}
                    return float(f.get("r5m",0)) + 0.7*float(f.get("r10m",0))
                weakest = min(room.items(), key=lambda kv: score_of(kv[1]))[0]
                room.pop(weakest, None)

            c = Coin(m, sym, ttl_sec)
            room[m] = c

        # تحديث CV
        c.cv.update({
            "r5m":  float(feat.get("r5m",  feat.get("r300",0.0))),
            "r10m": float(feat.get("r10m", feat.get("r600",0.0))),
            "volZ": float(feat.get("volZ", 0.0)),
            "spread_bp": float(feat.get("spread_bp", 0.0))
        })

        # زرع السعر الأولي إن وجد
        p0 = None
        try: p0 = float(feat.get("price_now") or 0.0)
        except: p0 = None
        if p0 and p0>0:
            c.last_price = p0
            if c.entry_price is None:
                c.entry_price = p0
            c.buf.append((nowt, p0))

        # جدّد TTL + سكون خفيف
        c.expires_at   = nowt + TTL_MIN*60
        c.silent_until = nowt + 5

# ======== الأسعار / سبريد ========
def fetch_price_single(market):
    """آمنة: استعلام مفرد — موثوقة على Bitvavo."""
    data = http_get("/v2/ticker/price", params={"market": market})
    try:
        if isinstance(data, dict):
            return float(data.get("price") or 0.0)
    except: pass
    # بديل: من /24h
    data24 = http_get("/v2/ticker/24h")
    if data24:
        it = next((x for x in data24 if x.get("market")==market), None)
        try: return float((it or {}).get("last", 0) or 0)
        except: return None
    return None

def spread_ok(market, fallback=SPREAD_MAX_BP):
    data24 = http_get("/v2/ticker/24h")
    if not data24: return True
    it = next((x for x in data24 if x.get("market")==market), None)
    if not it: return True
    try:
        bid = float(it.get("bid",0) or 0); ask = float(it.get("ask",0) or 0)
        if bid<=0 or ask<=0: return True
        bp = (ask - bid)/((ask+bid)/2)*10000
        return bp <= fallback
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
    return (hi - last) / hi * 100.0

# ======== قرار + إطلاق ========
def decide_and_alert():
    global last_global_alert
    nowt = now()

    with room_lock:
        # استبعاد العملة الخسرانة بقوة
        viable = []
        for m, c in room.items():
            if nowt >= c.expires_at: continue
            if c.since_in() <= NEG_SINCEIN_CUTOFF: 
                continue
            f = c.cv or {}
            score = float(f.get("r5m",0.0)) + 0.7*float(f.get("r10m",0.0))
            viable.append((score, m, c))
        viable.sort(reverse=True)
        top = viable[:max(0, ALERT_TOP_N)]

    for _, m, c in top:
        if nowt < c.silent_until: 
            continue
        if nowt - c.last_alert_at < ALERT_COOLDOWN_SEC:
            continue
        if nowt - last_global_alert < GLOBAL_ALERT_GAP:
            continue

        f = c.cv or {}
        r5m = float(f.get("r5m",0.0)); r10m = float(f.get("r10m",0.0))
        if r5m < R5M_MIN: 
            continue

        r20 = c.r_change(20); r40 = c.r_change(40)
        hi60 = recent_high(c, 60); dd60 = recent_dd_pct(c,60)
        price_now = c.last_price

        if price_now is None or hi60 is None:
            continue

        nudge_ok   = (r20 >= NUDGE_R20 and r40 >= NUDGE_R40)
        breakout_ok= (price_now > hi60 * (1.0 + BREAKOUT_BP/10000.0))
        dd_ok      = (dd60 <= DD60_MAX)
        spread_okay= spread_ok(m)

        if not (dd_ok and spread_okay and (nudge_ok or breakout_ok)):
            continue

        # أطلق
        c.last_alert_at = nowt
        last_global_alert = nowt
        saqar_buy(c.symbol)

# ======== مراقبة الأسعار ========
def monitor_loop():
    rr = 0
    while True:
        try:
            with room_lock:
                markets = list(room.keys())
            if not markets:
                time.sleep(SCAN_SEC); continue

            batch = markets[rr:rr+BATCH_SIZE] or markets[:BATCH_SIZE]
            rr = (rr + BATCH_SIZE) % max(1,len(markets))

            for m in batch:
                p = fetch_price_single(m)
                if p is None: 
                    continue
                ts = now()
                with room_lock:
                    c = room.get(m)
                    if not c: continue
                    c.last_price = p
                    if c.entry_price is None:
                        c.entry_price = p
                    c.buf.append((ts, p))

            decide_and_alert()
        except Exception as e:
            print("[MONITOR] error:", e)
        time.sleep(SCAN_SEC)

# ======== واجهات ========
def build_status_text():
    with room_lock:
        rows = []
        nowt = now()
        items = []
        for m, c in room.items():
            f = c.cv or {}
            score = float(f.get("r5m",0.0)) + 0.7*float(f.get("r10m",0.0))
            items.append((score, m, c))
        items.sort(reverse=True)

        for rank, (_, m, c) in enumerate(items, start=1):
            f = c.cv or {}
            r5m  = float(f.get("r5m",0.0))
            r10m = float(f.get("r10m",0.0))
            r20  = c.r_change(20); r60 = c.r_change(60); r120 = c.r_change(120)
            vz   = float(f.get("volZ", 0.0))
            since = c.since_in()
            ttl = int(max(0, c.expires_at - nowt))
            star = "⭐" if rank <= ALERT_TOP_N else " "
            rows.append(f"{rank:02d}.{star} {m:<10} | r5m {r5m:+.2f}%  r10m {r10m:+.2f}% "
                        f"r20 {r20:+.2f}%  r60 {r60:+.2f}%  r120 {r120:+.2f}%  "
                        f"SinceIn {since:+.2f}%  volZ {vz:+.2f}  Buf{len(c.buf)}  TTL {ttl}s")
    hdr = f"📊 Room {len(room)}/{ROOM_CAP} | TopN={ALERT_TOP_N} | Gap={GLOBAL_ALERT_GAP}s"
    return hdr + ("\n" + "\n".join(rows) if rows else "\n(لا يوجد عملات بعد)")

app = Flask(__name__)

@app.route("/")
def root():
    return "Winner Hunter B is alive ✅"

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
def tg_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg  = data.get("message") or data.get("edited_message") or {}
        txt  = (msg.get("text") or "").strip().lower()
        chat = msg.get("chat", {}).get("id")
        if txt in ("status","/status","الحالة","/الحالة"):
            tg_send(build_status_text(), chat_id=chat or CHAT_ID)
        return jsonify(ok=True)
    except Exception as e:
        print("[WEBHOOK] err:", e); return jsonify(ok=True)

# ======== الإقلاع ========
def wipe_redis_on_start():
    if not rds: return
    try:
        keys = list(rds.scan_iter("*"))
        if keys:
            rds.delete(*keys)
        print(f"[REDIS] wiped {len(keys)} keys")
    except Exception as e:
        print("[REDIS] wipe error:", e)

def start_threads():
    wipe_redis_on_start()
    threading.Thread(target=monitor_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)