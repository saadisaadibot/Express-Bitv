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

# --- انحياز 24h بسيط ---
DAILY_EASE_MAX_24H   = float(os.getenv("DAILY_EASE_MAX_24H", 5.0))   # إذا d24 ≤ هذا → تسهيل
DAILY_TIGHT_MIN_24H  = float(os.getenv("DAILY_TIGHT_MIN_24H", 25.0)) # إذا d24 ≥ هذا → تصعيب
EASE_M_FACTOR        = float(os.getenv("EASE_M_FACTOR", 0.70))       # m_local = m * 0.70
TIGHT_M_FACTOR       = float(os.getenv("TIGHT_M_FACTOR", 1.20))      # m_local = m * 1.20

# === عتبات Exploder Score ===
TH_TOP1              = float(os.getenv("TH_TOP1", 70.0))         # شرط إضافي لنمط top1
TH_TOP10             = float(os.getenv("TH_TOP10", 62.0))        # شرط إضافي لنمط top10

# دفتر الأوامر — حدود سيولة/سبريد عامة (تُستخدم داخل السكور)
OB_MAX_SPREAD_BP     = float(os.getenv("OB_MAX_SPREAD_BP", 60.0))
OB_MIN_BID_EUR_REF   = float(os.getenv("OB_MIN_BID_EUR_REF", 500.0)) # مرجع لتطبيع السيولة
OB_CACHE_SEC         = int(os.getenv("OB_CACHE_SEC", 5))

# كاش الشموع
CANDLES_CACHE_SEC    = int(os.getenv("CANDLES_CACHE_SEC", 10))

# تصفية عملات "وهمية"/مستقرة (اختياري)
EXCLUDE_STABLES      = int(os.getenv("EXCLUDE_STABLES", 1))
STABLE_SET = {"USDT","USDC","DAI","TUSD","FDUSD","EUR","EURS","USDe","PYUSD","XAUT"}

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

# كاش تغيّر 24h / الشموع / دفتر الأوامر
_daily24_cache = {}   # coin -> {"pct": float, "ts": epoch}
_candles_cache = {}   # (coin, interval) -> {"data": list, "ts": epoch}
_ob_cache      = {}   # market -> {"data": dict, "ts": epoch}

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
    """
    قراءة تغير 24 ساعة من Bitvavo (/ticker/24h) مع كاش لمدة 60 ثانية.
    يرجّع نسبة مئوية (float).
    """
    now = time.time()
    rec = _daily24_cache.get(coin)
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
    _daily24_cache[coin] = {"pct": pct, "ts": now}
    return pct

def get_5m_top_symbols(limit=MAX_ROOM):
    """
    نجمع أفضل العملات بفريم 5m اعتمادًا على السلسلة المحلية (فرق الإغلاق الحالي عن إغلاق قبل ~5m).
    نعتمد فقط أسواق EUR وبحالة trading، ونستبعد العملات المستقرة (اختياري).
    """
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return []

    symbols = []
    try:
        markets = resp.json()
        for m in markets:
            if m.get("quote") != "EUR" or m.get("status") != "trading":
                continue
            base = m.get("base")
            if not base or not base.isalpha() or len(base) > 6:
                continue
            if EXCLUDE_STABLES and base.upper() in STABLE_SET:
                continue
            symbols.append(base)
    except Exception:
        pass

    now = time.time()
    changes = []
    for base in symbols:
        dq = prices[base]
        # نبحث عن نقطة قبل 300±30 ثانية
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

        # حدّث السلسلة
        dq.append((now, cur))
        # نحافظ على 20 دقيقة تقريبًا
        cutoff = now - 1200
        while dq and dq[0][0] < cutoff:
            dq.pop(0)

    changes.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in changes[:limit]]

def get_rank_from_bitvavo(coin):
    """
    ترتيب العملة لحظيًا ضمن Top حسب تغيّر 5m المحلي.
    إذا ما قدر يحدده، يرجع 999.
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
        cur = dq[-1][1] if dq else get_price(c)
        if cur is None:
            continue
        ch = ((cur - old) / old * 100.0) if old else 0.0
        scores.append((c, ch))

    scores.sort(key=lambda x: x[1], reverse=True)
    rank_map = {sym:i+1 for i,(sym,_) in enumerate(scores)}
    return rank_map.get(coin, 999)

# =========================
# 📚 دفتر أوامر + ميزاته (مع كاش)
# =========================
def fetch_orderbook(market):
    now = time.time()
    rec = _ob_cache.get(market)
    if rec and now - rec["ts"] < OB_CACHE_SEC:
        return rec["data"]
    resp = http_get(f"{BASE_URL}/{market}/book", timeout=6)
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
        if data and data.get("bids") and data.get("asks"):
            _ob_cache[market] = {"data": data, "ts": now}
            return data
    except Exception:
        pass
    return None

def ob_features(market):
    """
    يرجّع dict: {spread_bp, bid_eur, imb}
    """
    ob = fetch_orderbook(market)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        return None
    try:
        bid_p, bid_q = float(ob["bids"][0][0]), float(ob["bids"][0][1])
        ask_p, ask_q = float(ob["asks"][0][0]), float(ob["asks"][0][1])
    except Exception:
        return None
    spread_bp = (ask_p - bid_p) / ((ask_p + bid_p)/2.0) * 10000.0
    bid_eur   = bid_p * bid_q
    imb       = bid_q / max(1e-9, ask_q)
    return {"spread_bp": spread_bp, "bid_eur": bid_eur, "imb": imb}

# =========================
# 🧾 نص الحالة
# =========================
def build_status_text():
    def pct_change_from_lookback(dq, lookback_sec, now_ts):
        if not dq:
            return 0.0
        cur = dq[-1][1]
        old = None
        for ts, pr in reversed(dq):
            if now_ts - ts >= lookback_sec:
                old = pr
                break
        if old and old > 0:
            return (cur - old) / old * 100.0
        return 0.0

    def drawdown_20m(dq, now_ts):
        if not dq:
            return 0.0
        cur = dq[-1][1]
        mx = max(pr for ts, pr in dq if now_ts - ts <= 1200) if dq else None
        if mx and mx > 0:
            return (cur - mx) / mx * 100.0
        return 0.0

    now = time.time()
    rows = []
    for c in list(watchlist):
        dq = prices[c]
        if not dq:
            continue
        r1m  = pct_change_from_lookback(dq, 60,  now)
        r5m  = pct_change_from_lookback(dq, 300, now)
        r15m = pct_change_from_lookback(dq, 900, now)
        dd20 = drawdown_20m(dq, now)
        rank = get_rank_from_bitvavo(c)
        rows.append((c, r1m, r5m, r15m, dd20, rank))

    rows.sort(key=lambda x: x[2], reverse=True)
    lines = []
    lines.append(f"📊 غرفة المراقبة: {len(watchlist)}/{MAX_ROOM} | Heat={heat_ewma:.2f}")
    if not rows:
        lines.append("— لا توجد بيانات كافية بعد.")
        return "\n".join(lines)

    for i, (c, r1m, r5m, r15m, dd20, rank) in enumerate(rows, 1):
        lines.append(
            f"{i:02d}. {c} #top{rank} | r1m {r1m:+.2f}% | r5m {r5m:+.2f}% | r15m {r15m:+.2f}% | DD20 {dd20:+.2f}%"
        )
        if i >= 30:
            break

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
    rank = get_rank_from_bitvavo(coin)
    if rank > RANK_FILTER:
        return
    # كولداون
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC:
        return
    last_alert[coin] = now

    msg = f"🚀 {coin} {tag} #top{rank}"
    if change_text:
        msg = f"🚀 {coin} {change_text} #{tag} #top{rank}"
    send_message(msg)

    # إلى صقر (اختياري)
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
    """
    حرارة السوق = نسبة العملات في الغرفة التي تحرّكت ≥ HEAT_RET_PCT خلال آخر 60ث.
    ثم نعمل EWMA لتنعيم القراءة.
    """
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
    # EWMA
    heat_ewma = (1-HEAT_SMOOTH)*heat_ewma + HEAT_SMOOTH*raw if total else heat_ewma
    return heat_ewma

def adaptive_multipliers():
    """
    يحوّل حرارة السوق [0..1+] إلى معامل تقريبي.
    """
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
# 🕯️ شموع + ATR + Vol
# =========================
def get_candles(coin, interval="1m", limit=60):
    """
    يرجّع قائمة شموع [[time, open, high, low, close, volume], ...]
    Bitvavo: GET /{market}/candles?interval=1m&limit=60
    مع كاش بسيط لتخفيف الضغط.
    """
    now = time.time()
    key = (coin, interval)
    rec = _candles_cache.get(key)
    if rec and now - rec["ts"] < CANDLES_CACHE_SEC:
        return rec["data"]

    market = f"{coin}-EUR"
    resp = http_get(f"{BASE_URL}/{market}/candles", {"interval": interval, "limit": limit}, timeout=8)
    data = []
    if resp and resp.status_code == 200:
        try:
            raw = resp.json()
            if isinstance(raw, list):
                data = raw
        except Exception:
            data = []
    _candles_cache[key] = {"data": data, "ts": now}
    return data

def _atr(candles, n=14):
    if len(candles) < n+1:
        return 0.0
    trs = []
    prev_close = float(candles[0][4])
    for c in candles[1:]:
        high = float(c[2]); low = float(c[3]); close = float(c[4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
        if len(trs) >= n:
            break
    return (sum(trs)/len(trs)) if trs else 0.0

# =========================
# 🧨 Exploder Score
# =========================
def exploder_score(coin):
    """
    سكور (0..100) يفضّل المرشّحين لانفجار قوي:
    - زخم 1د وتسارع قصير
    - انفجار حجم (آخر 5د مقابل خط أساس 30د)
    - توسع تذبذب (ATR قصير/طويل)
    - ضغط دفتر أوامر (imb جيد, spread ضيق, سيولة معقولة)
    - خروج من نطاق ضيق
    """
    now = time.time()
    dq = prices[coin]
    if not dq:
        return 0.0

    # r1m/r5m/r15m من deque
    def pct_back(lb):
        old = None; cur = dq[-1][1]
        for ts, pr in reversed(dq):
            if now - ts >= lb:
                old = pr; break
        return ((cur - old)/old*100.0) if (old and old > 0) else 0.0

    r1  = pct_back(60)
    r5  = pct_back(300)
    r15 = pct_back(900)

    # تسارع r1m: الآن مقابل r1m قبل 120ث
    r1_old = 0.0
    old_cur = None; old_old = None
    for ts, pr in reversed(dq):
        if now - ts >= 120:
            old_cur = pr; break
    if old_cur:
        for ts, pr in reversed(dq):
            if now - ts >= 180:
                old_old = pr; break
        if old_old and old_old > 0:
            r1_old = (old_cur - old_old)/old_old*100.0
    accel = r1 - r1_old

    # شموع 1m
    c1m = get_candles(coin, "1m", 60)

    # انفجار حجم: sum آخر 5د ÷ (متوسط 30د * 5)
    vol5 = sum(float(c[5]) for c in c1m[-5:]) if len(c1m) >= 5 else 0.0
    vol30 = (sum(float(c[5]) for c in c1m[-30:]) / 30.0) if len(c1m) >= 30 else 0.0
    vol_spike = (vol5 / (vol30*5.0)) if (vol30 and vol30 > 0) else 0.0  # baseline 5 دقائق

    # توسّع التذبذب: ATR قصير ÷ طويل
    atr_short = _atr(c1m[-20:], n=14) if len(c1m) >= 20 else 0.0
    atr_long  = _atr(c1m[-60:], n=14) if len(c1m) >= 60 else (atr_short or 1.0)
    vol_expansion = (atr_short/atr_long) if atr_long > 0 else 0.0

    # نطاق ضيق 30د ثم اختراق إيجابي
    def range_pct(candles, n):
        if len(candles) < n: return 0.0
        hi = max(float(c[2]) for c in candles[-n:])
        lo = min(float(c[3]) for c in candles[-n:])
        mid = (hi+lo)/2.0
        return ((hi-lo)/mid*100.0) if mid>0 else 0.0
    rng30 = range_pct(c1m, 30)
    tight_then_break = 1.0 if (rng30 <= 0.8 and r5 >= 1.0) else 0.0

    # دفتر الأوامر
    feats = ob_features(f"{coin}-EUR") or {"spread_bp":9999, "bid_eur":0, "imb":0}
    spread_ok = 1.0 if feats["spread_bp"] <= OB_MAX_SPREAD_BP else 0.0
    liq_ok    = min(1.0, feats["bid_eur"]/OB_MIN_BID_EUR_REF)  # ≥ مرجع → 1.0
    imb_ok    = min(1.0, feats["imb"]/2.0)                      # 2.0 → 1.0

    # عقوبة لو r15 سلبي قوي (خطر ارتداد)
    penalty = -10.0 if r15 <= -2.0 else 0.0

    score = (
        22.0 * max(0.0, min(1.5, r1/1.5)) +     # زخم 1م
        18.0 * max(0.0, min(1.5, accel/1.5)) +  # تسارع
        22.0 * max(0.0, min(2.0, vol_spike)) +  # انفجار حجم
        14.0 * max(0.0, min(2.0, vol_expansion)) +
        10.0 * tight_then_break +
        7.0  * spread_ok +
        5.0  * liq_ok +
        4.0  * imb_ok +
        penalty
    )

    return max(0.0, min(100.0, score))

# =========================
# 🧩 أنماط (top10 / top1) كما هي
# =========================
def check_top10_pattern(coin, m):
    """
    نمط 1% + 1% خلال STEP_WINDOW_SEC (متكيّف بالمعامل m).
    """
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
    """
    نمط قوي: تسلسل نسب مثل "2,1,2" خلال SEQ_WINDOW_SEC (متكيّف).
    """
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
                # نحافظ على الحجم الأقصى
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
            # 20 دقيقة احتفاظ
            cutoff = now - 1200
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
                # انحياز 24h ← يؤثر فقط على الم عامل الأنماط (مش على السكور)
                d24 = get_daily_change_pct(s)
                m_local = m
                if d24 <= DAILY_EASE_MAX_24H:
                    m_local = m * EASE_M_FACTOR
                elif d24 >= DAILY_TIGHT_MIN_24H:
                    m_local = m * TIGHT_M_FACTOR

                # احسب Exploder Score
                score = exploder_score(s)

                # نسمح بالإشعار فقط إذا تحقق النمط **وال** سكور ≥ العتبة
                if check_top1_pattern(s, m_local) and score >= TH_TOP1:
                    notify_buy(s, tag="top1", change_text=f"🔥 Exploder {score:.0f}")
                    continue

                if check_top10_pattern(s, m_local) and score >= TH_TOP10:
                    notify_buy(s, tag="top10", change_text=f"⚡ Exploder {score:.0f}")
        except Exception as e:
            print("analyzer error:", e)

        time.sleep(1)

# =========================
# 🌐 (اختياري) Webhook بسيط للفحص
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

    STATUS_ALIASES = {
        "الحالة", "/status", "/stats", "شو عم تعمل", "/شو_عم_تعمل", "status"
    }

    if text in STATUS_ALIASES:
        send_message(build_status_text())
        return "ok", 200

    # تجاهل أي رسائل أخرى
    return "ok", 200

# =========================
# 🚀 التشغيل
# =========================
_started = False
def start_workers_once():
    global _started
    if _started:
        return
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller,   daemon=True).start()
    Thread(target=analyzer,       daemon=True).start()
    _started = True

# شغّل الخيوط فور الاستيراد (يناسب Gunicorn)
start_workers_once()

# خيار إضافي: تأكيد عند أول طلب HTTP (ما بيضر)
@app.before_request
def _ensure_started():
    start_workers_once()

if __name__ == "__main__":
    # تشغيل محلي فقط
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))