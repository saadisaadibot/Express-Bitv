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
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))   # كولداون لكل عملة داخل الجلسة
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))   # مهلة إحماء بعد التشغيل
ALERT_EXPIRE_SEC     = int(os.getenv("ALERT_EXPIRE_SEC", 24*3600)) # عدم تكرار الإشعار 24 ساعة

# فلترة عملات ارتفعت آخر 12 ساعة
SKIP_12H_PCT         = float(os.getenv("SKIP_12H_PCT", 15.0))    # تخطّي العملات إذا كان صعود 12h ≥ هذا
SKIP_CACHE_SEC       = int(os.getenv("SKIP_CACHE_SEC", 3600))    # كاش نتيجة الفلتر 1 ساعة

# توصيلات
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
SAQAR_WEBHOOK        = os.getenv("SAQAR_WEBHOOK")                # اختياري: يرسل "اشتري COIN"
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# =========================
# 🧠 الحالة
# =========================
r = redis.from_url(REDIS_URL)
lock = Lock()
watchlist = set()                       # رموز مثل "ADA"
prices = defaultdict(lambda: deque())   # لكل رمز: deque[(ts, price)]
last_alert = {}                         # coin -> ts (داخل الجلسة فقط)
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
    resp = http_get(f"{BASE_URL}/ticker/price", {"market": f"{symbol}-EUR"})
    if not resp or resp.status_code != 200:
        return None
    try:
        return float(resp.json()["price"])
    except Exception:
        return None

# شموع 1h الصحيحة لحساب صعود آخر 12 ساعة
def get_candles_1h(symbol, limit=13):
    resp = http_get(f"{BASE_URL}/candles",
                    {"market": f"{symbol}-EUR", "interval": "1h", "limit": limit},
                    timeout=10)
    if not resp or resp.status_code != 200:
        return []
    try:
        return resp.json()  # [ts, open, high, low, close, vol]
    except Exception:
        return []

def rose_last_12h(symbol):
    """
    يرجع True إذا كان التغير (close_now / close_قبل 12 ساعة - 1)*100 ≥ SKIP_12H_PCT
    فيه كاش بالريدز لتخفيف الطلبات.
    """
    ck = f"skip12h:{symbol}"
    c = r.get(ck)
    if c is not None:
        return c == b"1"
    candles = get_candles_1h(symbol, limit=13)
    ok = False
    try:
        closes = [float(c[4]) for c in candles if len(c) >= 5]
        if len(closes) >= 2 and closes[0] > 0:
            ch = (closes[-1] - closes[0]) / closes[0] * 100.0
            ok = (ch >= SKIP_12H_PCT)
    except Exception:
        ok = False
    r.setex(ck, SKIP_CACHE_SEC, "1" if ok else "0")
    return ok

def get_5m_top_symbols(limit=MAX_ROOM):
    """
    نجمع أفضل العملات بفريم 5m اعتمادًا على الشموع المحلية (فرق الإغلاق الحالي عن إغلاق قبل ~5m).
    """
    # نجلب كل الأسواق مقابل اليورو
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return []

    symbols = []
    try:
        for m in resp.json():
            if m.get("quote") == "EUR" and m.get("status") == "trading":
                base = m.get("base")
                # لا نستبعد عملات فيها أرقام (1000SATS...) – خلّيها مفتوحة
                if base:
                    symbols.append(base)
    except Exception:
        pass

    # نحسب تغيّر 5m بالاعتماد على سعر الآن وسعر محفوظ قبل ~270ث
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

        # حدّث السلسلة
        dq.append((now, cur))
        cutoff = now - 900   # ~15 دقيقة
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # تخلّص من العملات التي ارتفعت 12h (من الآن) قبل إدخالها الغرفة
    filtered = []
    for sym, ch in changes:
        try:
            if rose_last_12h(sym):
                continue
        except Exception:
            pass
        filtered.append((sym, ch))

    filtered.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in filtered[:limit]]

def get_rank_from_bitvavo(coin):
    """
    ترتيب العملة ضمن الغرفة حسب تغيّر 5m المحلي.
    """
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
    return {sym:i+1 for i,(sym,_) in enumerate(scores)}.get(coin, 999)

# =========================
# 📣 إرسال الإشعارات
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text}, timeout=8)
    except Exception as e:
        print("Telegram error:", e)

def already_alerted_today(coin):  # عدم تكرار الإشعار 24 ساعة
    try:
        return r.exists(f"alerted:{coin}") == 1
    except Exception:
        return False

def mark_alerted_today(coin):
    try:
        r.setex(f"alerted:{coin}", ALERT_EXPIRE_SEC, "1")
    except Exception:
        pass

def notify_buy(coin, tag, change_text=None):
    # فلتر 12h: لا ترسل إشعار على عملة صاعدة بقوة خلال 12 ساعة
    if rose_last_12h(coin):
        return

    rank = get_rank_from_bitvavo(coin)
    if rank > RANK_FILTER:
        return

    # كولداون داخل الجلسة + عدم تكرار خلال 24h
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC:
        return
    if already_alerted_today(coin):
        return
    last_alert[coin] = now
    mark_alerted_today(coin)

    msg = f"🚀 {coin} {tag} #top{rank}"
    if change_text:
        msg = f"🚀 {coin} {change_text} #top{rank}"
    send_message(msg)

    # إلى صقر (اختياري)
    if SAQAR_WEBHOOK:
        try:
            requests.post(SAQAR_WEBHOOK,
                          json={"message": {"text": f"اشتري {coin}"}},
                          timeout=5)
        except Exception:
            pass

# =========================
# 🔥 حرارة السوق + تكييف
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
    if h < 0.15:   return 0.75
    if h < 0.35:   return 0.90
    if h < 0.60:   return 1.00
    return 1.25

# =========================
# 🧩 أنماط (top10 / top1)
# =========================
def check_top10_pattern(coin, m):
    thresh = BASE_STEP_PCT * m
    now = time.time()
    dq = prices[coin]
    if len(dq) < 2:
        return False
    start_ts = now - STEP_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3:
        return False

    p0 = window[0][1]
    step1 = False
    last_p = p0
    for ts, pr in window[1:]:
        ch1 = (pr - p0) / p0 * 100.0
        if not step1 and ch1 >= thresh:
            step1 = True
            last_p = pr
            continue
        if step1:
            ch2 = (pr - last_p) / last_p * 100.0
            if ch2 >= thresh:
                return True
            if (pr - last_p) / last_p * 100.0 <= -thresh:
                step1 = False
                p0 = pr
    return False

def check_top1_pattern(coin, m):
    seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    seq_parts = [x * m for x in seq_parts]
    now = time.time()
    dq = prices[coin]
    if len(dq) < 2:
        return False

    start_ts = now - SEQ_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3:
        return False

    slack = 0.3 * m
    base_p = window[0][1]
    step_i = 0
    peak_after_step = base_p

    for ts, pr in window[1:]:
        ch = (pr - base_p) / base_p * 100.0
        need = seq_parts[step_i]
        if ch >= need:
            step_i += 1
            base_p = pr
            peak_after_step = pr
            if step_i == len(seq_parts):
                return True
        else:
            if peak_after_step > 0:
                drop = (pr - peak_after_step) / peak_after_step * 100.0
                if drop <= -(slack):
                    base_p = pr
                    peak_after_step = pr
                    step_i = 0
    return False

# =========================
# 🔁 العمال
# =========================
def room_refresher():
    while True:
        try:
            new_syms = get_5m_top_symbols(limit=MAX_ROOM)
            with lock:
                # أضف الجديد
                for s in new_syms:
                    watchlist.add(s)
                # نظّف الغرفة من العملات التي ارتفعت 12h
                for s in list(watchlist):
                    try:
                        if rose_last_12h(s):
                            watchlist.discard(s)
                    except Exception:
                        pass
                # حافظ على الحجم
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
            time.sleep(1)
            continue
        try:
            compute_market_heat()
            m = adaptive_multipliers()

            with lock:
                syms = list(watchlist)

            for s in syms:
                # تخطّي العملات المرتفعة 12h
                if rose_last_12h(s):
                    continue
                if check_top1_pattern(s, m):
                    notify_buy(s, tag="top1"); continue
                if check_top10_pattern(s, m):
                    notify_buy(s, tag="top10")
        except Exception as e:
            print("analyzer error:", e)
        time.sleep(1)

# =========================
# 🌐 فحص بسيط
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

# =========================
# 🚀 التشغيل
# =========================
if __name__ == "__main__":
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller, daemon=True).start()
    Thread(target=analyzer, daemon=True).start()
    # ملاحظة: لو على Railway/Gunicorn، عادةً يفضّل ترك app.run للـ dev فقط
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))