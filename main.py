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

# --- فلاتر 24h / كبح المنفوخ / الانحياز للناشئة ---
DAILY_PUMP_BLOCK     = float(os.getenv("DAILY_PUMP_BLOCK", 28.0))  # % إذا 24h ≥ هذا ومعه زخم ضعيف → منع
POST_PEAK_RETRACE    = float(os.getenv("POST_PEAK_RETRACE", 1.5))  # % هبوط من القمة يمنع إعادة الإشعار
NEW_HIGH_MARGIN_BP   = float(os.getenv("NEW_HIGH_MARGIN_BP", 0.08))# % لازم نكسر قمة آخر 3د بهامش بسيط

NOVELTY_MIN_24H      = float(os.getenv("NOVELTY_MIN_24H", -5.0))
NOVELTY_MAX_24H      = float(os.getenv("NOVELTY_MAX_24H", 8.0))
NOVELTY_M_FACTOR     = float(os.getenv("NOVELTY_M_FACTOR", 0.8))   # تخفيض عتبات للعملات الناشئة

EXHAUST_COOLDOWN_SEC = int(os.getenv("EXHAUST_COOLDOWN_SEC", 1800)) # 30 دقيقة كولداون تعب
ALERTS_PER_HOUR_MAX  = int(os.getenv("ALERTS_PER_HOUR_MAX", 1))     # سقف إشعارات/عملة/ساعة

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
watchlist = set()                        # رموز مثل "ADA"
prices = defaultdict(lambda: deque())    # لكل رمز: deque[(ts, price)]
last_alert = {}                          # coin -> ts
heat_ewma = 0.0                          # حرارة السوق الملسّاة
start_time = time.time()

# حالات إضافية
daily_change_cache = {}                  # coin -> {"pct": float, "ts": epoch}
last_peak = defaultdict(lambda: 0.0)     # أعلى قمة منذ آخر تنشيط
exhausted_until = defaultdict(lambda: 0.0) # coin -> ts
alerts_counter = defaultdict(lambda: deque(maxlen=20))  # طوابع وقت آخر إشعارات

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

def get_daily_change_pct(coin):
    now = time.time()
    rec = daily_change_cache.get(coin)
    if rec and now - rec["ts"] < 60:
        return rec["pct"]
    market = f"{coin}-EUR"
    resp = http_get(f"{BASE_URL}/ticker/24h", {"market": market})
    pct = 0.0
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            pct = float(data.get("priceChangePercentage", 0.0))
        except Exception:
            pass
    daily_change_cache[coin] = {"pct": pct, "ts": now}
    return pct

def get_5m_top_symbols(limit=MAX_ROOM):
    # نجلب كل الأسواق مقابل اليورو
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

    # نحسب تغيّر 5m بالاعتماد على سعر الآن وسعر محفوظ قبل ~300ث
    now = time.time()
    changes = []
    for base in symbols:
        dq = prices[base]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270:  # ~4.5 د
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

        # حدّث السلسلة
        dq.append((now, cur))
        cutoff = now - 900  # ~15د
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
        if old:
            ch = (cur - old) / old * 100.0
        else:
            ch = 0.0
        scores.append((c, ch))

    scores.sort(key=lambda x: x[1], reverse=True)
    rank_map = {sym:i+1 for i,(sym,_) in enumerate(scores)}
    return rank_map.get(coin, 999)

# =========================
# 🧮 أدوات القياس المحلي
# =========================
def pct_change_from_lookback(dq, lookback_sec, now_ts):
    if not dq: return 0.0
    cur = dq[-1][1]
    old = None
    for ts, pr in reversed(dq):
        if now_ts - ts >= lookback_sec:
            old = pr; break
    if old and old > 0: return (cur - old) / old * 100.0
    return 0.0

def max_price_in_window(dq, lookback_sec, now_ts):
    if not dq: return 0.0
    lo = now_ts - lookback_sec
    mx = 0.0
    for ts, pr in dq:
        if ts >= lo: mx = max(mx, pr)
    return mx

# =========================
# 📊 النصوص/الحالة
# =========================
def build_status_text():
    def drawdown_20m(dq, now_ts):
        if not dq: return 0.0
        cur = dq[-1][1]
        mx = max((pr for ts, pr in dq if now_ts - ts <= 1200), default=None)
        if mx and mx > 0:
            return (cur - mx) / mx * 100.0
        return 0.0

    now = time.time()
    rows = []
    for c in list(watchlist):
        dq = prices[c]
        if not dq: continue
        r1m  = pct_change_from_lookback(dq, 60,  now)
        r5m  = pct_change_from_lookback(dq, 300, now)
        r15m = pct_change_from_lookback(dq, 900, now)
        dd20 = drawdown_20m(dq, now)
        rank = get_rank_from_bitvavo(c)
        rows.append((c, r1m, r5m, r15m, dd20, rank))

    rows.sort(key=lambda x: x[2], reverse=True)
    lines = [f"📊 غرفة المراقبة: {len(watchlist)}/{MAX_ROOM} | Heat={heat_ewma:.2f}"]
    if not rows:
        lines.append("— لا توجد بيانات كافية بعد.")
        return "\n".join(lines)

    for i, (c, r1m, r5m, r15m, dd20, rank) in enumerate(rows, 1):
        lines.append(f"{i:02d}. {c} #top{rank} | r1m {r1m:+.2f}% | r5m {r5m:+.2f}% | r15m {r15m:+.2f}% | DD20 {dd20:+.2f}%")
        if i >= 30: break
    return "\n".join(lines)

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

def notify_buy(coin, tag, change_text=None):
    # منع إعادة نفس القمة تقريبًا (تشديد اختياري)
    lp = last_peak.get(coin, 0.0)
    if lp > 0 and prices[coin]:
        cur = prices[coin][-1][1]
        if lp > 0 and abs(cur - lp) / lp * 100.0 < 0.05:
            return

    rank = get_rank_from_bitvavo(coin)
    if rank > RANK_FILTER:
        return

    # كولداون أساسي
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC:
        return
    last_alert[coin] = now

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
                old = pr; break
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
    if h < 0.15:   m = 0.75
    elif h < 0.35: m = 0.9
    elif h < 0.6:  m = 1.0
    else:          m = 1.25
    return m

# =========================
# 🧩 أنماط الإشعار (كما هي)
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
            cutoff = now - 1200  # ~20 دقيقة احتفاظ
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

            now = time.time()
            for s in syms:
                # كبح بسبب إنهاك سابق
                if exhausted_until[s] > now:
                    continue

                dq = prices[s]
                if not dq:
                    continue
                cur = dq[-1][1]

                # مقاييس لحظية
                r1m  = pct_change_from_lookback(dq, 60,  now)
                r5m  = pct_change_from_lookback(dq, 300, now)

                # 24h
                d24 = get_daily_change_pct(s)

                # أقصى قمة 20د لحساب DD (استخدام موضعي)
                mx20 = max_price_in_window(dq, 1200, now)
                dd20 = (cur - mx20) / mx20 * 100.0 if mx20 > 0 else 0.0

                # 1) بوابة “المنفوخ”: 24h ≥ DAILY_PUMP_BLOCK مع زخم لحظي ضعيف → منع + كولداون
                if d24 >= DAILY_PUMP_BLOCK and (r1m <= 0.0 or r5m <= 0.0 or dd20 <= -1.0):
                    exhausted_until[s] = now + EXHAUST_COOLDOWN_SEC
                    continue

                # 2) شرط قمة جديدة محلية (آخر 3 دقائق) بهامش بسيط
                recent_peak = max_price_in_window(dq, 180, now)
                need_break = recent_peak * (1.0 + NEW_HIGH_MARGIN_BP/100.0)
                if recent_peak > 0 and cur < need_break:
                    # لم نكسر قمة حديثة → تجنّب الإشعار الآن
                    continue

                # 3) كبح ما بعد القمة: هبوط X% من آخر قمة مسجلة → كولداون
                if last_peak[s] > 0:
                    draw = (cur - last_peak[s]) / last_peak[s] * 100.0
                    if draw <= -POST_PEAK_RETRACE:
                        exhausted_until[s] = now + EXHAUST_COOLDOWN_SEC
                        continue
                    if cur > last_peak[s]:
                        last_peak[s] = cur
                else:
                    last_peak[s] = cur  # تهيئة أولية

                # 4) انحياز “الناشئة”: تخفيض عتبات لو الديلي معتدل والتسارع موجود
                m_local = m
                if NOVELTY_MIN_24H <= d24 <= NOVELTY_MAX_24H and (r1m > 0.4 and r1m > r5m*0.6):
                    m_local = m * NOVELTY_M_FACTOR  # أسرع

                # 5) سقف إشعارات/ساعة لكل عملة
                while alerts_counter[s] and now - alerts_counter[s][0] > 3600:
                    alerts_counter[s].popleft()
                if len(alerts_counter[s]) >= ALERTS_PER_HOUR_MAX:
                    continue

                # ===== الأنماط (top1 أولاً ثم top10) =====
                fired = False
                if check_top1_pattern(s, m_local):
                    notify_buy(s, tag="top1")
                    alerts_counter[s].append(now)
                    fired = True
                elif check_top10_pattern(s, m_local):
                    notify_buy(s, tag="top10")
                    alerts_counter[s].append(now)
                    fired = True

                # تحديث القمة بعد الإطلاق
                if fired and cur > last_peak[s]:
                    last_peak[s] = cur

        except Exception as e:
            print("analyzer error:", e)
        time.sleep(1)

# =========================
# 🌐 Webhook/صحة
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

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return "ok", 200

    STATUS_ALIASES = {"الحالة", "/status", "/stats", "شو عم تعمل", "/شو_عم_تعمل", "status"}
    if text in STATUS_ALIASES:
        send_message(build_status_text())
        return "ok", 200

    return "ok", 200

# =========================
# 🚀 التشغيل
# =========================
_started = False
def start_workers_once():
    global _started
    if _started: return
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller,   daemon=True).start()
    Thread(target=analyzer,       daemon=True).start()
    _started = True

start_workers_once()

@app.before_request
def _ensure_started():
    start_workers_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))