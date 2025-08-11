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

# منع السبام / تكرار التنبيهات
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))   # كولداون احتياطي (لن نستخدمه إذا فعّلنا التنبيه-مرة-واحدة)
ALERT_EXPIRE_SEC     = int(os.getenv("ALERT_EXPIRE_SEC", 24*3600)) # مدة منع التكرار لكل عملة (افتراضي يوم كامل)
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))   # مهلة إحماء بعد التشغيل

# فلترة “انفجرت آخر يوم” و“الميتة تتنفس”
LASTDAY_SKIP_PCT     = float(os.getenv("LASTDAY_SKIP_PCT", 15.0)) # تجاهل إذا 24h change ≥ هذا الحد
REVIVE_ONLY          = int(os.getenv("REVIVE_ONLY", 0))           # 1 = لا نُرسل إلا للعملات الميتة التي بدأت تتنفس
REVIVE_CACHE_SEC     = int(os.getenv("REVIVE_CACHE_SEC", 3600))   # كاش قرار revive لكل عملة
CANDLES_LIMIT_1H     = int(os.getenv("CANDLES_LIMIT_1H", 168))    # 7 أيام * 24 ساعة

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
last_alert = {}                         # coin -> ts (احتياطي)
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

def get_24h_change(symbol):
    """
    تغيّر 24h % من Bitvavo.
    نُخزّنه 5 دقائق لتخفيف الضغط.
    """
    ck = f"ch24:{symbol}"
    cached = r.get(ck)
    if cached is not None:
        try:
            return float(cached)
        except:
            pass
    market = f"{symbol}-EUR"
    resp = http_get(f"{BASE_URL}/ticker/24h", {"market": market})
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
        ch = float(data.get("priceChangePercentage", "0") or 0)
        r.setex(ck, 300, str(ch))
        return ch
    except:
        return None

def get_candles_1h(symbol, limit=CANDLES_LIMIT_1H):
    market = f"{symbol}-EUR"
    # Bitvavo: /markets/{market}/candles?interval=1h&limit=...
    resp = http_get(f"{BASE_URL}/markets/{market}/candles", {"interval": "1h", "limit": limit}, timeout=10)
    if not resp or resp.status_code != 200:
        return []
    try:
        # كل شمعة: [timestamp, open, high, low, close, volume]
        return resp.json()
    except:
        return []

def is_recent_exploder(symbol):
    """
    True إذا كانت العملة 'منفجرة' خلال آخر 24 ساعة (≥ LASTDAY_SKIP_PCT).
    """
    ch24 = get_24h_change(symbol)
    return (ch24 is not None) and (ch24 >= LASTDAY_SKIP_PCT)

def is_reviving(symbol):
    """
    'ميّتة وعم تتنفس':
    - خلال 7 أيام: لم تُظهر ارتفاعات حادة (هدوء/ركود) → max(returns) <= ~15%
    - خلال 24h: ليست مرتفعة أصلاً (ch24 < 8%)
    ملاحظة: شروط مرنة جدًا وقابلة للتعديل لاحقًا.
    """
    key = f"revive:{symbol}"
    cached = r.get(key)
    if cached is not None:
        return cached.decode() == "1"

    candles = get_candles_1h(symbol)
    if len(candles) < 24:  # بيانات قليلة => ما نحكم
        r.setex(key, REVIVE_CACHE_SEC, "0")
        return False

    closes = [float(c[4]) for c in candles if len(c) >= 5]
    if len(closes) < 24:
        r.setex(key, REVIVE_CACHE_SEC, "0")
        return False

    base = closes[0]
    max_up = 0.0
    for c in closes[1:]:
        if base > 0:
            ch = (c - base) / base * 100.0
            if ch > max_up:
                max_up = ch

    ch24 = get_24h_change(symbol) or 0.0
    ok = (max_up <= 15.0) and (ch24 < 8.0)  # هادئة أسبوعيًا + ليست مرتفعة يوميًا
    r.setex(key, REVIVE_CACHE_SEC, "1" if ok else "0")
    return ok

def get_5m_top_symbols(limit=MAX_ROOM):
    """
    نجمع أفضل العملات بفريم 5m اعتمادًا على الشموع (فرق الإغلاق الحالي عن إغلاق قبل ~5m).
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
        if old:
            ch = (cur - old) / old * 100.0
        else:
            ch = 0.0
        changes.append((base, ch))
        dq.append((now, cur))
        cutoff = now - 900
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    changes.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in changes[:limit]]

def get_rank_from_bitvavo(coin):
    """
    ترتيب العملة لحظيًا ضمن Top حسب تغيّر 5m المحلي (من deque).
    إذا ما قدر يحدده، يرجع رقم كبير (خارج التوب).
    """
    now = time.time()
    scores = []
    # نوسع الترتيب على كل الغرفة وليس فقط العملة
    with lock:
        wl = list(watchlist)
    for c in wl:
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
    rank_map = {sym: i+1 for i, (sym, _) in enumerate(scores)}
    return rank_map.get(coin, 999)

# =========================
# 📣 إرسال الإشعارات + السجل
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text}, timeout=6)
    except Exception as e:
        print("Telegram error:", e)

def log_alert(entry: dict):
    try:
        r.lpush("alerts", json.dumps(entry, ensure_ascii=False))
        r.ltrim("alerts", 0, 49)  # احتفظ بآخر 50 فقط
    except Exception as e:
        print("log_alert error:", e)

def already_alerted_today(coin):
    return r.exists(f"alerted:{coin}") == 1

def mark_alerted_today(coin):
    r.setex(f"alerted:{coin}", ALERT_EXPIRE_SEC, "1")

def notify_buy(coin, tag, change_text=None):
    # منع التكرار “مرة واحدة باليوم”
    if already_alerted_today(coin):
        return

    # فلترة: إذا منفجرة آخر يوم → تجاهل
    if is_recent_exploder(coin):
        return

    # فلترة “الميتة تتنفس”
    revive_ok = is_reviving(coin)
    if REVIVE_ONLY and not revive_ok:
        return

    rank = get_rank_from_bitvavo(coin)
    if rank > RANK_FILTER:
        return

    # كولداون احتياطي (لن يعمل غالباً لأننا نستخدم once/day)
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC:
        return
    last_alert[coin] = now

    mark_alerted_today(coin)

    tag_txt = f"{tag}"
    if revive_ok:
        tag_txt += " • revive"

    msg = f"🚀 {coin} {tag_txt} #top{rank}"
    if change_text:
        msg = f"🚀 {coin} {change_text} #top{rank}"

    send_message(msg)

    # إلى صقر (اختياري)
    if SAQAR_WEBHOOK:
        try:
            payload = {"message": {"text": f"اشتري {coin}"}}
            requests.post(SAQAR_WEBHOOK, json=payload, timeout=5)
        except Exception:
            pass

    # سجل
    log_alert({
        "ts": int(now),
        "coin": coin,
        "rank": rank,
        "tag": tag_txt,
        "heat": round(heat_ewma, 4)
    })

# =========================
# 🔥 حرارة السوق + تكييف العتبات
# =========================
def compute_market_heat():
    global heat_ewma
    now = time.time()
    moved = 0
    total = 0
    with lock:
        wl = list(watchlist)
    for c in wl:
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
    if h < 0.15:
        m = 0.75
    elif h < 0.35:
        m = 0.9
    elif h < 0.6:
        m = 1.0
    else:
        m = 1.25
    return m

# =========================
# 🧩 منطق الأنماط (top10 / top1)
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

    slack = 0.3 * m  # سماحية تراجع بسيطة
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
            time.sleep(1)
            continue

        try:
            compute_market_heat()
            m = adaptive_multipliers()

            with lock:
                syms = list(watchlist)

            for s in syms:
                # نمط top1 أولًا (أقوى)
                if check_top1_pattern(s, m):
                    notify_buy(s, tag="top1")
                    continue
                # ثم top10
                if check_top10_pattern(s, m):
                    notify_buy(s, tag="top10")
        except Exception as e:
            print("analyzer error:", e)
        time.sleep(1)

# =========================
# 🌐 مسارات مساعدة
# =========================
@app.route("/", methods=["GET"])
def health():
    return "Predictor bot is alive ✅", 200

@app.route("/stats", methods=["GET"])
def stats():
    with lock:
        wl = list(watchlist)
    return {
        "watchlist": wl,
        "heat": round(heat_ewma, 4),
        "roomsz": len(wl)
    }, 200

@app.route("/status", methods=["GET"])
def status():
    # “شو عم يعمل؟” مختصر
    m = adaptive_multipliers()
    with lock:
        wl = list(watchlist)
    return {
        "message": "المحلّل يعمل ويطبّق top1 ثم top10 مع منع تكرار يومي وتنظيف المنفجرة 24h وإعطاء أولوية للميّتة المتنفسة.",
        "heat": round(heat_ewma, 4),
        "multiplier": m,
        "watchlist_size": len(wl),
        "rank_filter": RANK_FILTER,
        "lastday_skip_pct": LASTDAY_SKIP_PCT,
        "revive_only": bool(REVIVE_ONLY)
    }, 200

@app.route("/sajel", methods=["GET"])
def sajel():
    # يعرض آخر 50 تنبيه
    items = []
    for raw in r.lrange("alerts", 0, 49):
        try:
            items.append(json.loads(raw))
        except:
            pass
    return {"alerts": items}, 200

# =========================
# 🚀 التشغيل
# =========================
if __name__ == "__main__":
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller, daemon=True).start()
    Thread(target=analyzer, daemon=True).start()
    # ملاحظة: لو على Railway/Gunicorn، يفضل عدم استخدام app.run()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))