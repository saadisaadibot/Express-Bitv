# -*- coding: utf-8 -*-
import os, time, json, math, requests, redis
from collections import deque, defaultdict
from threading import Thread, Lock
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))        # كل كم ثانية نقرأ الأسعار
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 180)) # كل كم ثانية نحدّث الغرفة
MAX_ROOM             = int(os.getenv("MAX_ROOM", 20))            # حجم غرفة المراقبة
RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))         # لا إشعار إلا إذا Top N عند الإرسال

# أنماط الإشعار الأساسية (قبل التكييف)
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))    # نمط top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")     # نمط top1: 2% ثم 1% ثم 2% خلال 5 دقائق
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))     # نافذة النمط القوي (ثواني)
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))    # نافذة 1% + 1% (ثواني)

# تكييف حسب حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))  # نقيس الحرارة عبر آخر دقيقتين
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))     # كم % خلال 60 ث لنحسبها حركة
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))      # EWMA لنعومة الحرارة

# منع السبام
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))   # كولداون لكل عملة
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))   # مهلة إحماء بعد التشغيل

# فلتر آخر 24 ساعة
LASTDAY_SKIP_PCT     = float(os.getenv("LASTDAY_SKIP_PCT", 15.0))  # تجاهل إذا تغيّر 24h ≥ هذا الحد

# توصيلات
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
SAQAR_WEBHOOK        = os.getenv("SAQAR_WEBHOOK")  # اختياري: يرسل "اشتري COIN"
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# =========================
# 🧠 الحالة
# =========================
r = redis.from_url(REDIS_URL)
lock = Lock()
watchlist = set()                       # رموز مثل "ADA"
prices = defaultdict(lambda: deque())   # لكل رمز: deque[(ts, price)]
last_alert = {}                         # coin -> ts
heat_ewma = 0.0                         # حرارة السوق الملسّاة
start_time = time.time()

# =========================
# 🛰️ دوال مساعدة (Bitvavo)
# =========================
BASE_URL = "https://api.bitvavo.com/v2"

def http_get(url, params=None, timeout=8):
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except Exception:
            time.sleep(0.5)
    return None

def get_price(symbol):  # symbol مثل "ADA"
    market = f"{symbol}-EUR"
    resp = http_get(f"{BASE_URL}/ticker/price", {"market": market})
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
        return float(data["price"])
    except Exception:
        return None

# تغيّر آخر 24 ساعة (٪)
def get_24h_change(symbol):
    resp = http_get(f"{BASE_URL}/ticker/24h", {"market": f"{symbol}-EUR"})
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
        # Bitvavo يرجّع priceChangePercentage كنص
        return float(data.get("priceChangePercentage", "0") or 0)
    except Exception:
        return None

def exploded_last_24h(symbol):
    """True إذا تغيّر 24h ≥ LASTDAY_SKIP_PCT."""
    ch = get_24h_change(symbol)
    return (ch is not None) and (ch >= LASTDAY_SKIP_PCT)

def get_5m_top_symbols(limit=MAX_ROOM):
    """
    نجمع أفضل العملات بفريم 5m اعتمادًا على الشموع (فرق الإغلاق الحالي عن إغلاق قبل ~5m)،
    مع استثناء العملات التي انفجرت 24h.
    """
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return []

    symbols = []
    try:
        markets = resp.json()
        for m in markets:
            if m.get("quote") == "EUR" and m.get("status") == "trading":
                base = m.get("base")
                if base and base.isalpha() and len(base) <= 6:
                    # فلتر انفجار 24h
                    if exploded_last_24h(base):
                        continue
                    symbols.append(base)
    except Exception:
        pass

    now = time.time()
    changes = []
    for base in symbols:
        dq = prices[base]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270:
                old = pr
                break
        cur = get_price(base)
        if cur is None:
            continue
        ch = (cur - old) / old * 100.0 if old else 0.0
        changes.append((base, ch))

        dq.append((now, cur))
        cutoff = now - 900
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    changes.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in changes[:limit]]

def get_rank_from_bitvavo(coin):
    now = time.time()
    scores = []
    for c in list(watchlist):
        dq = prices[c]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270:
                old = pr
                break
        cur = prices[c][-1][1] if dq else get_price(c)
        if cur is None:
            continue
        ch = (cur - old) / old * 100.0 if old else 0.0
        scores.append((c, ch))
    scores.sort(key=lambda x: x[1], reverse=True)
    rank_map = {sym:i+1 for i,(sym,_) in enumerate(scores)}
    return rank_map.get(coin, 999)

# =========================
# 📣 إرسال الإشعارات
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("Telegram error:", e)

# منع تكرار الإشعار "إلى الأبد" عبر Redis Set
def already_notified(coin):
    try:
        return r.sismember("alerted_forever", coin)
    except Exception:
        return False

def mark_notified(coin):
    try:
        r.sadd("alerted_forever", coin)
    except Exception:
        pass

def notify_buy(coin, tag, change_text=None):
    if exploded_last_24h(coin):  # فلتر 24h قبل كل شيء
        return
    if already_notified(coin):   # لا نعيد الإشعار أبداً
        return

    rank = get_rank_from_bitvavo(coin)
    if rank > RANK_FILTER:
        return

    # كولداون داخل الجلسة (زيادة أمان، ما بتأثر على "أبداً")
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC:
        return
    last_alert[coin] = now
    mark_notified(coin)

    msg = f"🚀 {coin} {tag} #top{rank}"
    if change_text:
        msg = f"🚀 {coin} {change_text} #top{rank}"
    send_message(msg)

    if SAQAR_WEBHOOK:
        try:
            payload = {"message": {"text": f"اشتري {coin}"}}
            requests.post(SAQAR_WEBHOOK, json=payload, timeout=5)
        except Exception:
            pass

# =========================
# 🔥 حرارة السوق + تكييف العتبات
# =========================
def compute_market_heat():
    global heat_ewma
    now = time.time()
    moved = 0
    total = 0
    for c in list(watchlist):
        dq = prices[c]
        if len(dq) < 2:
            continue
        old = None
        cur = dq[-1][1]
        for ts, pr in reversed(dq):
            if now - ts >= 60:
                old = pr
                break
        if old and old > 0:
            ret = (cur - old) / old * 100.0
            total += 1
            if abs(ret) >= HEAT_RET_PCT:
                moved += 1
    raw = (moved / total) if total else 0.0
    heat_ewma = (1-HEAT_SMOOTH)*heat_ewma + HEAT_SMOOTH*raw if total else heat_ewma
    return heat_ewma

def adaptive_multipliers():
    h = max(0.0, min(1.0, heat_ewma))
    if h < 0.15: m = 0.75
    elif h < 0.35: m = 0.9
    elif h < 0.6: m = 1.0
    else: m = 1.25
    return m

# =========================
# 🧩 أنماط (top10 / top1)
# =========================
def check_top10_pattern(coin, m):
    thresh = BASE_STEP_PCT * m
    now = time.time()
    dq = prices[coin]
    if len(dq) < 2: return False
    start_ts = now - STEP_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3: return False

    p0 = window[0][1]
    step1 = False
    last_p = p0
    for ts, pr in window[1:]:
        ch1 = (pr - p0) / p0 * 100.0
        if not step1 and ch1 >= thresh:
            step1 = True; last_p = pr; continue
        if step1:
            ch2 = (pr - last_p) / last_p * 100.0
            if ch2 >= thresh: return True
            if (pr - last_p) / last_p * 100.0 <= -thresh:
                step1 = False; p0 = pr
    return False

def check_top1_pattern(coin, m):
    seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    seq_parts = [x * m for x in seq_parts]
    now = time.time()
    dq = prices[coin]
    if len(dq) < 2: return False
    start_ts = now - SEQ_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3: return False

    slack = 0.3 * m
    base_p = window[0][1]
    step_i = 0
    peak_after_step = base_p
    for ts, pr in window[1:]:
        ch = (pr - base_p) / base_p * 100.0
        need = seq_parts[step_i]
        if ch >= need:
            step_i += 1; base_p = pr; peak_after_step = pr
            if step_i == len(seq_parts): return True
        else:
            if peak_after_step > 0:
                drop = (pr - peak_after_step) / peak_after_step * 100.0
                if drop <= -(slack):
                    base_p = pr; peak_after_step = pr; step_i = 0
    return False

# =========================
# 🔁 العمال
# =========================
def room_refresher():
    while True:
        try:
            new_syms = get_5m_top_symbols(limit=MAX_ROOM)
            with lock:
                for s in new_syms:
                    watchlist.add(s)
                if len(watchlist) > MAX_ROOM:
                    ranked = sorted(list(watchlist), key=lambda c: get_rank_from_bitvavo(c))
                    watchlist.clear()
                    for c in ranked[:MAX_ROOM]:
                        watchlist.add(c)
        except Exception as e:
            print("room_refresher error:", e)
        time.sleep(BATCH_INTERVAL_SEC)

def price_poller():
    while True:
        now = time.time()
        with lock:
            syms = list(watchlist)
        for s in syms:
            pr = get_price(s)
            if pr is None:
                continue
            dq = prices[s]
            dq.append((now, pr))
            cutoff = now - 1200  # 20 دقيقة
            while dq and dq[0][0] < cutoff:
                dq.popleft()
        time.sleep(SCAN_INTERVAL)

def analyzer():
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1); continue
        try:
            compute_market_heat(); m = adaptive_multipliers()
            with lock: syms = list(watchlist)
            for s in syms:
                if exploded_last_24h(s):  # فلتر 24h عند التحليل أيضاً
                    continue
                if already_notified(s):
                    continue
                if check_top1_pattern(s, m):
                    notify_buy(s, tag="top1"); continue
                if check_top10_pattern(s, m):
                    notify_buy(s, tag="top10")
        except Exception as e:
            print("analyzer error:", e)
        time.sleep(1)

# =========================
# 🌐 مسارات فحص
# =========================
@app.route("/", methods=["GET"])
def health():
    return "Predictor bot is alive ✅", 200

@app.route("/stats", methods=["GET"])
def stats():
    return {
        "watchlist": list(watchlist),
        "heat": round(heat_ewma, 4),
        "roomsz": len(watchlist)
    }, 200

# امر الحالة: يعرض قائمة الغرفة مرتبة حسب تغيّر 5m الحالي (بسيط وخفيف)
@app.route("/status", methods=["GET"])
def status():
    now = time.time()
    rows = []
    wl = list(watchlist)
    for c in wl:
        dq = prices[c]
        if not dq: 
            continue
        cur = dq[-1][1]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270:
                old = pr; break
        ch5m = ((cur - old) / old * 100.0) if (old and old > 0) else 0.0
        rows.append((c, round(cur, 8), round(ch5m, 3)))
    rows.sort(key=lambda x: x[2], reverse=True)
    return {
        "message": "OK",
        "watchlist_size": len(wl),
        "heat": round(heat_ewma, 4),
        "lastday_skip_pct": LASTDAY_SKIP_PCT,
        "list": [{"coin": c, "price": p, "ret_5m_pct": r} for c,p,r in rows]
    }, 200

# =========================
# 🚀 التشغيل
# =========================
if __name__ == "__main__":
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller, daemon=True).start()
    Thread(target=analyzer, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))