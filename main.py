# -*- coding: utf-8 -*-
import os, time, json, math, requests, redis, threading
from collections import deque, defaultdict
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))         # كل كم ثانية نقرأ الأسعار
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 180))  # كل كم ثانية نحدّث الغرفة
MAX_ROOM             = int(os.getenv("MAX_ROOM", 20))             # أقصى حجم للغرفة
RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))          # لا إشعار إلا إذا ضمن التوب
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))     # نمط top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")      # نمط top1: 2% ثم 1% ثم 2%
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))      # نافذة النمط القوي
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))     # نافذة 1% + 1%

# تكيّف حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))   # (معلومة) مدى القياس
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))      # تحرّك معتبر خلال 60s
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))       # EWMA نعومة الحرارة

# مكافحة السبام
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))    # كولداون لكل عملة
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))    # إحماء بعد التشغيل
GLOBAL_ALERT_GAP     = int(os.getenv("GLOBAL_ALERT_GAP", 7))      # فجوة بين أي إشعارين

# وضع التشغيل الافتراضي (normal/aggressive/calm)
MODE_DEFAULT         = os.getenv("MODE_DEFAULT", "normal").lower()

# توصيلات
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
SAQAR_WEBHOOK        = os.getenv("SAQAR_WEBHOOK")                 # اختياري: يرسل "اشتري COIN"
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# شبكة
BASE_URL             = "https://api.bitvavo.com/v2"
HTTP_TIMEOUT         = float(os.getenv("HTTP_TIMEOUT", 8.0))
PER_REQUEST_GAP_SEC  = float(os.getenv("PER_REQUEST_GAP_SEC", 0.08))

# =========================
# 🧠 الحالة
# =========================
r = redis.from_url(REDIS_URL)
lock = threading.Lock()
watchlist = set()                               # رموز مثل "ADA"
prices = defaultdict(lambda: deque(maxlen=2000))# (ts, price) للذاكرة الحية
last_alert = {}                                 # coin -> ts آخر إشعار
last_global_alert_ts = 0.0
heat_ewma = 0.0
start_time = time.time()

# وضع التشغيل (يُحفظ في Redis)
MODE_KEY = "predictor:mode"
_mode_cached = (r.get(MODE_KEY) or MODE_DEFAULT.encode()).decode().lower()
if _mode_cached not in ("aggressive", "normal", "calm"):
    _mode_cached = "normal"
current_mode = _mode_cached

# =========================
# 🌐 جلسة HTTP (Retries + Backoff)
# =========================
session = requests.Session()
retry = Retry(
    total=3, backoff_factor=0.3,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=["GET", "POST"]
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=64)
session.mount("https://", adapter)
session.mount("http://", adapter)

def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    try:
        resp = session.get(url, params=params, timeout=timeout)
        time.sleep(PER_REQUEST_GAP_SEC)  # gap خفيف
        return resp
    except Exception:
        return None

# =========================
# 🛰️ Bitvavo helpers
# =========================
def get_price(symbol):  # "ADA" -> float or None
    market = f"{symbol}-EUR"
    resp = http_get(f"{BASE_URL}/ticker/price", {"market": market})
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
        return float(data["price"])
    except Exception:
        return None

def list_eur_markets():
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return []
    out = []
    try:
        for m in resp.json():
            if m.get("quote") == "EUR" and m.get("status") == "trading":
                base = m.get("base")
                if base and base.isalpha() and len(base) <= 6:
                    out.append(base)
    except Exception:
        pass
    return out

# =========================
# 📈 قياسات من الذاكرة
# =========================
def get_ret(symbol, seconds):
    """تغيّر السعر % خلال نافذة ثوانٍ من deque المحلي فقط."""
    dq = prices[symbol]
    if not dq:
        return None
    now = time.time()
    cur = dq[-1][1]
    old = None
    for ts, pr in reversed(dq):
        if now - ts >= seconds:
            old = pr
            break
    if old is None or old <= 0:
        return None
    return (cur - old) / old * 100.0

def get_5m_top_symbols(limit=MAX_ROOM):
    """اختيار أفضل العملات بفريم 5m اعتمادًا على البيانات المحلية."""
    bases = list_eur_markets()
    now = time.time()
    changes = []
    for base in bases:
        pr = get_price(base)
        if pr is None:
            continue
        prices[base].append((now, pr))
        ch5m = get_ret(base, 300)
        changes.append((base, ch5m if ch5m is not None else 0.0))
    changes.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in changes[:limit]]

def rank_in_watchlist(coin):
    """ترتيب العملة ضمن الغرفة حسب تغيّر 5m المحلي."""
    with lock:
        syms = list(watchlist)
    scores = []
    for c in syms:
        ch5m = get_ret(c, 300)
        scores.append((c, ch5m if ch5m is not None else -9999.0))
    scores.sort(key=lambda x: x[1], reverse=True)
    idx = {s:i+1 for i,(s,_) in enumerate(scores)}
    return idx.get(coin, 999)

# =========================
# 🎛️ وضع المزاج/العدوانية
# =========================
def set_mode(new_mode: str):
    global current_mode
    new_mode = (new_mode or "").lower()
    if new_mode not in ("aggressive", "normal", "calm"):
        return False
    with lock:
        current_mode = new_mode
        r.set(MODE_KEY, new_mode)
    return True

def mode_params():
    """
    نُرجع معاملات حسب الوضع الحالي:
    - mode_mult: يضرب العتبات (أصغر = أسهل)
    - rank_delta: تعديل على RANK_FILTER (+ يوسّع, - يضيّق)
    - gap_delta: تعديل على GLOBAL_ALERT_GAP (ثوانٍ)
    - coin_cd_delta: تعديل على BUY_COOLDOWN_SEC (ثوانٍ)
    """
    if current_mode == "aggressive":
        return dict(mode_mult=0.70, rank_delta=+3, gap_delta=-3, coin_cd_delta=-(BUY_COOLDOWN_SEC*2//3))
    elif current_mode == "calm":
        return dict(mode_mult=1.25, rank_delta=-2, gap_delta=+5, coin_cd_delta=+300)
    # normal
    return dict(mode_mult=1.00, rank_delta=0, gap_delta=0, coin_cd_delta=0)

def effective_threshold_mult(heat_mult: float):
    """العتبة الفعلية = (حرارة السوق) × (وضع المزاج)."""
    return max(0.4, min(2.0, heat_mult * mode_params()["mode_mult"]))

def effective_rank_filter():
    rf = RANK_FILTER + mode_params()["rank_delta"]
    return max(3, min(MAX_ROOM, rf))

def effective_global_gap():
    return max(2, GLOBAL_ALERT_GAP + mode_params()["gap_delta"])

def effective_coin_cooldown():
    cd = BUY_COOLDOWN_SEC + mode_params()["coin_cd_delta"]
    return max(180, cd)

# =========================
# 📣 إشعارات
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}")
        return
    try:
        session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=HTTP_TIMEOUT
        )
    except Exception as e:
        print("Telegram error:", e)

def notify_buy(coin, tag, change_text=None):
    global last_global_alert_ts
    now = time.time()

    # فجوة عامة
    if now - last_global_alert_ts < effective_global_gap():
        return

    # كولداون العملة
    cd = effective_coin_cooldown()
    if coin in last_alert and now - last_alert[coin] < cd:
        return

    rank = rank_in_watchlist(coin)
    if rank > effective_rank_filter():
        return

    last_alert[coin] = now
    last_global_alert_ts = now

    msg = f"🚀 {coin} {tag} #top{rank}"
    if change_text:
        msg = f"🚀 {coin} {change_text} #top{rank}"
    send_message(msg)

    if SAQAR_WEBHOOK:
        try:
            payload = {"message": {"text": f"اشتري {coin}"}}
            session.post(SAQAR_WEBHOOK, json=payload, timeout=5)
        except Exception:
            pass

# =========================
# 🔥 حرارة السوق + تكيّف
# =========================
def compute_market_heat():
    """نسبة عملات الغرفة التي تحركت ≥ HEAT_RET_PCT خلال آخر 60s."""
    global heat_ewma
    with lock:
        syms = list(watchlist)
    moved, total = 0, 0
    for c in syms:
        ret60 = get_ret(c, 60)
        if ret60 is None:
            continue
        total += 1
        if abs(ret60) >= HEAT_RET_PCT:
            moved += 1
    raw = (moved / total) if total else 0.0
    heat_ewma = (1 - HEAT_SMOOTH) * heat_ewma + HEAT_SMOOTH * raw if total else heat_ewma
    return heat_ewma

def adaptive_multipliers_from_heat():
    """خرط حرارة السوق إلى معامل: بارد 0.75x .. ناري 1.25x."""
    h = max(0.0, min(1.0, heat_ewma))
    if h < 0.15:   m = 0.75
    elif h < 0.35: m = 0.90
    elif h < 0.60: m = 1.00
    else:          m = 1.25
    return m

# =========================
# 🧩 أنماط الإشارات
# =========================
def check_top10_pattern(coin, mult):
    """نمط 1% + 1% خلال STEP_WINDOW_SEC (متكيّف)."""
    thresh = BASE_STEP_PCT * mult
    now = time.time()
    dq = prices[coin]
    if len(dq) < 3:
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

def check_top1_pattern(coin, mult):
    """تسلسل قوي: "2,1,2" خلال SEQ_WINDOW_SEC (متكيّف)."""
    try:
        seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    except Exception:
        seq_parts = [2.0, 1.0, 2.0]
    seq_parts = [x * mult for x in seq_parts]

    now = time.time()
    dq = prices[coin]
    if len(dq) < 3:
        return False

    start_ts = now - SEQ_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3:
        return False

    slack = 0.3 * mult
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
                if drop <= -slack:
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
                ranked = sorted(list(watchlist), key=lambda c: rank_in_watchlist(c))
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
            prices[s].append((now, pr))
        time.sleep(SCAN_INTERVAL)

def analyzer():
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1); continue
        try:
            compute_market_heat()
            heat_mult = adaptive_multipliers_from_heat()
            mult = effective_threshold_mult(heat_mult)

            with lock:
                syms = list(watchlist)

            for s in syms:
                if check_top1_pattern(s, mult):
                    ch5 = get_ret(s, 300)
                    notify_buy(s, tag="top1", change_text=(f"top1 +{ch5:.2f}%" if ch5 is not None else "top1"))
                    continue
                if check_top10_pattern(s, mult):
                    ch5 = get_ret(s, 300)
                    notify_buy(s, tag="top10", change_text=(f"top10 +{ch5:.2f}%" if ch5 is not None else "top10"))
        except Exception as e:
            print("analyzer error:", e)
        time.sleep(1)

# =========================
# 📥 Webhook تيليجرام (أوامر خفيفة)
# =========================
@app.route("/tg", methods=["POST"])
def telegram_webhook():
    data = request.json
    if not data or "message" not in data:
        return "ok"
    text = (data["message"].get("text") or "").strip().lower()

    if text in ("/status", "status", "شو عم تعمل", "/شو_عم_تعمل"):
        send_status(); return "ok"

    if text in ("خليك عدواني", "/aggressive", "aggressive"):
        ok = set_mode("aggressive")
        send_message("⚡️ الوضع: عدواني — شروط أسهل قليلًا، توسيع TopN، تقليص الفجوة والكولداون.") if ok else None
        return "ok"

    if text in ("خليك عادي", "/normal", "normal"):
        ok = set_mode("normal")
        send_message("⚙️ الوضع: عادي — الإعدادات القياسية.") if ok else None
        return "ok"

    if text in ("خليك رايق", "/calm", "calm"):
        ok = set_mode("calm")
        send_message("🧊 الوضع: رايق — شروط أشد، تضييق TopN، زيادة الفجوة والكولداون.") if ok else None
        return "ok"

    if text in ("/mode", "mode", "الوضع"):
        send_message(f"الوضع الحالي: {current_mode}")
        return "ok"

    return "ok"

def send_status():
    with lock:
        wl = list(watchlist)
    heat_val = round(heat_ewma, 4)

    ranks = []
    for c in wl:
        ch5 = get_ret(c, 300)
        if ch5 is not None:
            ranks.append((c, ch5))
    ranks.sort(key=lambda x: x[1], reverse=True)
    top5 = [f"{i+1:02d}. {c}: {ch:.2f}%" for i, (c, ch) in enumerate(ranks[:5])]

    msg = (
        f"📊 Room {len(wl)}/{MAX_ROOM} | Heat={heat_val:.2f} | Mode={current_mode}\n" +
        ("\n".join(top5) if top5 else "(no data)")
    )
    send_message(msg)

# =========================
# 🌐 Health/Stats
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
        "roomsz": len(wl),
        "heat": round(heat_ewma, 4),
        "mode": current_mode,
        "rank_filter_eff": effective_rank_filter(),
        "global_gap_eff": effective_global_gap(),
        "coin_cd_eff": effective_coin_cooldown(),
        "cooldowns_tracked": len(last_alert),
    }, 200

# =========================
# 🚀 التشغيل
# =========================
if __name__ == "__main__":
    threading.Thread(target=room_refresher, daemon=True).start()
    threading.Thread(target=price_poller, daemon=True).start()
    threading.Thread(target=analyzer, daemon=True).start()
    # على Railway/Gunicorn لا تستخدم app.run()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))