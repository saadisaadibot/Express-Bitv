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
RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))         # (يبقى للاحتياط، صار عندنا ديناميكي)

# أنماط الإشعار
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))    # نمط top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")     # نمط top1: 2% ثم 1% ثم 2% خلال 5 دقائق
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))     # نافذة النمط القوي
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))    # نافذة 1% + 1%

# تكيّف حسب حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))

# منع السبام
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))

# (1) حارس الفتيل الكاذب
RETRACE_LIMIT        = float(os.getenv("RETRACE_LIMIT", -0.6))   # % نزول مسموح خلال فترة الثبات
STABILITY_SEC        = int(os.getenv("STABILITY_SEC", 20))       # آخر كم ثانية نطلب فيها ثبات

# (2) طرد المتأخرين
RANK_EVICT           = int(os.getenv("RANK_EVICT", 20))          # إذا الرتبة أسوأ من هذا
EVICT_GRACE_SEC      = int(os.getenv("EVICT_GRACE_SEC", 300))    # مهلة قبل الطرد

# (3) كاشف الانتعاش (عملة ميتة تنتعش فجأة)
REVIVE_ENABLE            = int(os.getenv("REVIVE_ENABLE", 1))
REVIVE_QUIET_MINUTES     = int(os.getenv("REVIVE_QUIET_MINUTES", 20))   # هدوء سابق
REVIVE_MAX_STD_PCT       = float(os.getenv("REVIVE_MAX_STD_PCT", 0.35)) # تذبذب ضعيف خلال الهدوء
REVIVE_1M_PCT            = float(os.getenv("REVIVE_1M_PCT", 1.2))       # قفزة دقيقة
REVIVE_3M_PCT            = float(os.getenv("REVIVE_3M_PCT", 2.5))       # قفزة 3 دقائق
REVIVE_RANK_ALLOW        = int(os.getenv("REVIVE_RANK_ALLOW", 20))      # رتبة مسموحة للإشعار
SEED_WARMUP_MINUTES      = int(os.getenv("SEED_WARMUP_MINUTES", 6))     # بذرة شاملة بالبداية
SEED_ROOM_SIZE           = int(os.getenv("SEED_ROOM_SIZE", 120))        # عدد عملات البذرة

# توصيلات
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
SAQAR_WEBHOOK        = os.getenv("SAQAR_WEBHOOK")                # اختياري
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# =========================
# 🧠 الحالة
# =========================
r = redis.from_url(REDIS_URL)
lock = Lock()
watchlist = set()                         # رموز مثل "ADA"
prices = defaultdict(lambda: deque())     # لكل رمز: deque[(ts, price)]
last_alert = {}                           # coin -> ts
last_bad_rank = {}                        # (2) تتبّع مدة سوء الرتبة
heat_ewma = 0.0
start_time = time.time()

# 📝 سجل أفعال لحظي
ACTION_LOG_MAX = int(os.getenv("ACTION_LOG_MAX", 300))  # أقصى عدد أسطر بالسجل
action_log = deque(maxlen=ACTION_LOG_MAX)

# =========================
# 🛰️ Bitvavo helpers
# =========================
BASE_URL = "https://api.bitvavo.com/v2"

def http_get(url, params=None, timeout=8):
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except Exception:
            time.sleep(0.5)
    return None

def get_price(symbol):
    market = f"{symbol}-EUR"
    resp = http_get(f"{BASE_URL}/ticker/price", {"market": market})
    if not resp or resp.status_code != 200:
        return None
    try:
        return float(resp.json()["price"])
    except Exception:
        return None

def get_5m_top_symbols(limit=MAX_ROOM):
    # نجلب أسواق EUR
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return []

    symbols = []
    try:
        for m in resp.json():
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
            if now - ts >= 270:  # ~5m - سماحية
                old = pr
                break
        cur = get_price(base)
        if cur is None:
            continue
        ch = ((cur - old) / old * 100.0) if old else 0.0
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
        cur = (dq[-1][1] if dq else get_price(c))
        if cur is None:
            continue
        ch = ((cur - old) / old * 100.0) if old else 0.0
        scores.append((c, ch))
    scores.sort(key=lambda x: x[1], reverse=True)
    return {sym: i+1 for i, (sym, _) in enumerate(scores)}.get(coin, 999)

# =========================
# 📣 إشعارات + سجل
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

def log_action(event, coin=None, extra=None):
    ts = time.strftime('%H:%M:%S', time.localtime())
    item = {"t": ts, "e": event}
    if coin:
        item["c"] = coin
    if extra is not None:
        item["x"] = extra
    action_log.append(item)

# (3) فلترة الرتبة ديناميكيًا بحسب حرارة السوق
def dyn_rank_filter():
    h = max(0.0, min(1.0, heat_ewma))
    if h < 0.2:
        return max(RANK_FILTER, 13)   # سوق بارد: نسمح أوسع
    elif h < 0.5:
        return 10
    else:
        return min(RANK_FILTER, 8)    # سوق مولّع: نكون صارمين

# (1) حارس الفتيل الكاذب: تأكد من ثبات قصير قبل الإرسال
def stable_lately(coin):
    now = time.time()
    dq = prices[coin]
    if len(dq) < 3:
        return True
    window = [p for (ts, p) in dq if now - ts <= STABILITY_SEC]
    if len(window) < 2:
        return True
    peak = max(window)
    cur = window[-1]
    dd = (cur - peak) / peak * 100.0
    return dd >= RETRACE_LIMIT

def notify_buy(coin, tag, change_text=None):
    # فلترة الرتبة ديناميكيًا
    rank = get_rank_from_bitvavo(coin)
    if rank > dyn_rank_filter():
        return

    # كولداون بسيط لكل عملة
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC:
        return

    # ثبات قصير (حارس الفتيل الكاذب)
    if not stable_lately(coin):
        return

    last_alert[coin] = now
    msg = f"🚀 {coin} {tag} #top{rank}"
    if change_text:
        msg = f"🚀 {coin} {change_text} #top{rank}"
    send_message(msg)
    log_action("alert", coin, {"tag": tag, "rank": rank})

    if SAQAR_WEBHOOK:
        try:
            requests.post(SAQAR_WEBHOOK,
                          json={"message": {"text": f"اشتري {coin}"}},
                          timeout=5)
        except Exception as e:
            log_action("error_webhook", coin, str(e))

# =========================
# 🔥 حرارة السوق + التكييف
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
    if h < 0.15:
        return 0.75
    elif h < 0.35:
        return 0.9
    elif h < 0.6:
        return 1.0
    else:
        return 1.25

# =========================
# 🧩 منطق الأنماط
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

# ======== أدوات كاشف الانتعاش ========
def pct_change_over(coin, seconds):
    now = time.time()
    dq = prices[coin]
    if not dq:
        return None
    cur_ts, cur_p = dq[-1]
    base = None
    for ts, pr in reversed(dq):
        if now - ts >= seconds:
            base = pr
            break
    if base is None or base <= 0:
        return None
    return (cur_p - base) / base * 100.0

def std_pct_last(coin, seconds):
    now = time.time()
    vals = [p for (ts, p) in prices[coin] if now - ts <= seconds]
    if len(vals) < 5:
        return None
    avg = sum(vals)/len(vals)
    if avg <= 0:
        return None
    var = sum((p-avg)**2 for p in vals)/len(vals)
    std = math.sqrt(var)
    return (std/avg)*100.0

# =========================
# 🔁 العمال
# =========================
seed_until = time.time() + SEED_WARMUP_MINUTES*60

def room_refresher():
    while True:
        try:
            if time.time() < seed_until:
                # بذرة واسعة: أضف أكبر قدر من رموز EUR للتاريخ المبكّر
                resp = http_get(f"{BASE_URL}/markets")
                syms = []
                if resp and resp.status_code == 200:
                    for m in resp.json():
                        if m.get("quote")=="EUR" and m.get("status")=="trading":
                            b = m.get("base")
                            if b and b.isalpha() and len(b)<=6:
                                syms.append(b)
                syms = syms[:SEED_ROOM_SIZE]
                with lock:
                    for s in syms:
                        if s not in watchlist:
                            watchlist.add(s)
                            log_action("seed_add", s)
            else:
                # السلوك الأصلي (Top 5m)
                new_syms = get_5m_top_symbols(limit=MAX_ROOM)
                with lock:
                    for s in new_syms:
                        if s not in watchlist:
                            watchlist.add(s)
                            log_action("room_add", s)
                    if len(watchlist) > MAX_ROOM:
                        ranked = sorted(list(watchlist), key=lambda c: get_rank_from_bitvavo(c))
                        watchlist.clear()
                        for c in ranked[:MAX_ROOM]:
                            watchlist.add(c)
                        log_action("room_rebalance", extra={"size": len(watchlist)})
        except Exception as e:
            print("room_refresher error:", e)
            log_action("error_room_refresher", extra=str(e))
        time.sleep(BATCH_INTERVAL_SEC)

def price_poller():
    while True:
        now = time.time()
        try:
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
        except Exception as e:
            print("price_poller error:", e)
            log_action("error_price_poller", extra=str(e))
        time.sleep(SCAN_INTERVAL)

def analyzer():
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1); continue
        try:
            compute_market_heat()
            m = adaptive_multipliers()

            with lock:
                syms = list(watchlist)

            for s in syms:
                # (2) منطق طرد المتأخرين
                rk = get_rank_from_bitvavo(s)
                if rk > RANK_EVICT:
                    last_bad_rank.setdefault(s, time.time())
                    if time.time() - last_bad_rank[s] >= EVICT_GRACE_SEC:
                        with lock:
                            watchlist.discard(s)
                        log_action("evict_rank", s, {"rank": rk})
                        last_bad_rank.pop(s, None)
                        continue
                else:
                    last_bad_rank.pop(s, None)

                # --- Revive detector (عملة ميتة تنتعش فجأة) ---
                if REVIVE_ENABLE:
                    rk_allow = REVIVE_RANK_ALLOW
                    if rk <= rk_allow:
                        ch1 = pct_change_over(s, 60)    # دقيقة
                        ch3 = pct_change_over(s, 180)   # 3 دقائق
                        quiet = std_pct_last(s, REVIVE_QUIET_MINUTES*60)
                        if (ch1 is not None and ch3 is not None and quiet is not None
                            and quiet <= REVIVE_MAX_STD_PCT
                            and ch1 >= REVIVE_1M_PCT and ch3 >= REVIVE_3M_PCT
                            and stable_lately(s)):
                            log_action("revive_hit", s, {"ch1m": round(ch1,2),
                                                         "ch3m": round(ch3,2),
                                                         "quiet_std%": round(quiet,2)})
                            notify_buy(s, tag="revive")
                            continue

                # اكتشاف الأنماط الأساسية
                if check_top1_pattern(s, m):
                    log_action("top1_hit", s, {"mult": m})
                    notify_buy(s, tag="top1"); continue
                if check_top10_pattern(s, m):
                    log_action("top10_hit", s, {"mult": m})
                    notify_buy(s, tag="top10")
        except Exception as e:
            print("analyzer error:", e)
            log_action("error_analyzer", extra=str(e))
        time.sleep(1)

# =========================
# 🌐 فحوصات + واجهات
# =========================
@app.route("/", methods=["GET"])
def health():
    return "Predictor bot is alive ✅", 200

@app.route("/heat", methods=["GET"])
def heat_info():
    return {
        "market_heat": round(heat_ewma, 4),            # حرارة السوق الحالية
        "dyn_rank_limit": dyn_rank_filter(),           # الحد المسموح للرتبة حاليًا
        "pattern_multiplier": adaptive_multipliers(),  # معامل الأنماط الحالي
        "watchlist": list(watchlist),                  # قائمة العملات في الغرفة
        "watchlist_size": len(watchlist),              # عدد العملات في الغرفة
        "last_alerts": {
            k: time.strftime('%H:%M:%S', time.localtime(v))
            for k, v in last_alert.items()
        }
    }, 200

@app.route("/sajel", methods=["GET"])
def trade_log():
    # ?limit=50 للحد من النتائج (افتراضي 30)
    try:
        limit = int(request.args.get("limit", 30))
    except:
        limit = 30
    limit = max(1, min(limit, ACTION_LOG_MAX))
    data = list(action_log)[-limit:][::-1]  # أحدث أولاً
    return {
        "count": len(data),
        "max": ACTION_LOG_MAX,
        "items": data
    }, 200

# =========================
# 🚀 التشغيل
# =========================
if __name__ == "__main__":
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller, daemon=True).start()
    Thread(target=analyzer, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))