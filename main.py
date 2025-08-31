# -*- coding: utf-8 -*-
import os, time, json, math, requests, redis, threading, traceback, re
from collections import deque, defaultdict
from threading import Thread, Lock
from flask import Flask, request
from dotenv import load_dotenv

# =========================
# Boot
# =========================
load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 2))            # REST bulk tick
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 45))      # كان 180
MAX_ROOM             = int(os.getenv("MAX_ROOM", 30))
RANK_FILTER          = int(os.getenv("RANK_FILTER", 15))             # لا إشعار إلا إذا Top N عند الإرسال (إلا إذا force)

# أنماط الإشعار الأساسية
BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))        # نمط top10: 1% + 1%
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")         # نمط top1: 2 → 1 → 2 خلال نافذة
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))

# WS و التحليل السريع
USE_WS               = int(os.getenv("USE_WS", 1))
WS_STALENESS_SEC     = float(os.getenv("WS_STALENESS_SEC", 1.0))
ANALYZER_TICK_SEC    = float(os.getenv("ANALYZER_TICK_SEC", 0.5))    # سرعة التحليل

# Booster (كسر الكولداون إذا زادت القوة بوضوح)
BOOST_ENABLE         = int(os.getenv("BOOST_ENABLE", 1))
BOOST_SCORE_GAP      = float(os.getenv("BOOST_SCORE_GAP", 5.0))      # قفزة سكّور لإعادة التنبيه
BOOST_MIN_DELAY_SEC  = int(os.getenv("BOOST_MIN_DELAY_SEC", 30))     # أقل زمن بين التنبيهين

# تكييف حرارة السوق
HEAT_LOOKBACK_SEC    = int(os.getenv("HEAT_LOOKBACK_SEC", 120))
HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))

# منع السبام
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))       # كولداون افتراضي
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 10))

# انحياز 24h (خفّفنا التشديد)
DAILY_EASE_MAX_24H   = float(os.getenv("DAILY_EASE_MAX_24H", 5.0))
DAILY_TIGHT_MIN_24H  = float(os.getenv("DAILY_TIGHT_MIN_24H", 40.0))  # كان 25.0
EASE_M_FACTOR        = float(os.getenv("EASE_M_FACTOR", 0.70))
TIGHT_M_FACTOR       = float(os.getenv("TIGHT_M_FACTOR", 1.05))       # كان 1.20

# عتبات Exploder
TH_TOP1              = float(os.getenv("TH_TOP1", 60.0))
TH_TOP10             = float(os.getenv("TH_TOP10", 50.0))
TH_PRE               = float(os.getenv("TH_PRE", 40.0))               # حد أدنى لإشارة pre

# دفتر الأوامر (أوسع قليلاً للإدراجات)
OB_MAX_SPREAD_BP     = float(os.getenv("OB_MAX_SPREAD_BP", 100.0))    # كان 60.0
OB_MIN_BID_EUR_REF   = float(os.getenv("OB_MIN_BID_EUR_REF", 500.0))
OB_CACHE_SEC         = int(os.getenv("OB_CACHE_SEC", 5))

# كاش الشموع
CANDLES_CACHE_SEC    = int(os.getenv("CANDLES_CACHE_SEC", 10))

# Pre-Exploder (سِكويز + تسارع + حجم/دفتر أوامر) — مرنة أكثر
PRE_R10_MIN          = float(os.getenv("PRE_R10_MIN", 0.40))          # % خلال 10 ثوان
PRE_ACCEL_MIN        = float(os.getenv("PRE_ACCEL_MIN", 0.25))        # r10 - r30
PRE_VOLSPIKE_MIN     = float(os.getenv("PRE_VOLSPIKE_MIN", 0.5))      # vol5 / (avg30*5)
PRE_TIGHT_RANGE_MAX  = float(os.getenv("PRE_TIGHT_RANGE_MAX", 1.5))   # كان 0.8

# تصفية المستقرة
EXCLUDE_STABLES      = int(os.getenv("EXCLUDE_STABLES", 1))
STABLE_SET = {"USDT","USDC","DAI","TUSD","FDUSD","EUR","EURS","USDe","PYUSD","XAUT"}

# توصيلات
BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
SAQAR_WEBHOOK        = os.getenv("SAQAR_WEBHOOK")  # يرسل "اشتري COIN"
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# =========================
# 🧠 الحالة
# =========================
r = redis.from_url(REDIS_URL)
lock = Lock()
watchlist = set()                        # رموز "ADA"
prices = defaultdict(lambda: deque())    # coin -> deque[(ts, price)]
last_alert_ts = {}                       # coin -> ts
last_alert_info = {}                     # coin -> {"ts":..., "score":..., "tag":...}
heat_ewma = 0.0
start_time = time.time()

_daily24_cache = {}                      # coin -> {"pct": float, "ts": epoch}
_candles_cache = {}                      # (coin, interval) -> {"data": list, "ts": epoch}
_ob_cache      = {}                      # market -> {"data": dict, "ts": epoch}

# =========================
# 🛰️ Bitvavo helpers
# =========================
BASE_URL = "https://api.bitvavo.com/v2"

def http_get(url, params=None, timeout=8):
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except Exception:
            time.sleep(0.3)
    return None

# ---- Bulk price cache ----
PX_CACHE_SEC = float(os.getenv("PX_CACHE_SEC", "1.5"))
_px_cache = {"ts": 0.0, "map": {}}

def refresh_bulk_prices():
    resp = http_get(f"{BASE_URL}/ticker/price", timeout=6)
    if not resp or resp.status_code != 200:
        return False
    try:
        arr = resp.json()
        mp = {}
        for it in arr:
            m = it.get("market"); p = it.get("price")
            if not m or not p:
                continue
            try:
                mp[m] = float(p)
            except:
                pass
        _px_cache["ts"] = time.time()
        _px_cache["map"] = mp
        return True
    except Exception:
        return False

def get_price(symbol):
    """يقرأ السعر من كاش bulk (يُحدّث تلقائياً إذا تقادم)."""
    now = time.time()
    if now - _px_cache["ts"] > PX_CACHE_SEC:
        refresh_bulk_prices()
    return _px_cache["map"].get(f"{symbol}-EUR")

def get_daily_change_pct(coin):
    now = time.time()
    rec = _daily24_cache.get(coin)
    if rec and now - rec["ts"] < 60:
        return rec["pct"]
    resp = http_get(f"{BASE_URL}/ticker/24h", {"market": f"{coin}-EUR"})
    pct = 0.0
    if resp and resp.status_code == 200:
        try:
            pct = float(resp.json().get("priceChangePercentage", 0.0))
        except Exception:
            pass
    _daily24_cache[coin] = {"pct": pct, "ts": now}
    return pct

# --- كل أزواج EUR (للتغذية الشاملة) ---
ALL_EUR = {"ts": 0.0, "syms": []}
ALL_EUR_TTL = 600

def get_all_eur_symbols():
    now = time.time()
    if ALL_EUR["syms"] and now - ALL_EUR["ts"] < ALL_EUR_TTL:
        return ALL_EUR["syms"]
    resp = http_get(f"{BASE_URL}/markets")
    syms = []
    if resp and resp.status_code == 200:
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
                syms.append(base)
        except Exception:
            pass
    ALL_EUR.update({"ts": now, "syms": syms})
    return syms

def get_5m_top_symbols(limit=MAX_ROOM):
    symbols = get_all_eur_symbols()
    now = time.time()
    changes = []

    # نحدّث كاش الأسعار مرة واحدة ونستخدمه
    refresh_bulk_prices()
    pxmap = dict(_px_cache["map"])

    LOOKBACK = 120  # كان 270s
    for base in symbols:
        dq = prices[base]
        if not dq:  # ماعنا نقاط كافية بعد
            pr = pxmap.get(f"{base}-EUR")
            if pr is not None:
                dq.append((now, pr))
            continue
        cur = dq[-1][1]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= LOOKBACK:
                old = pr; break
        if old and old > 0:
            ch = (cur - old) / old * 100.0
            changes.append((base, ch))

        # تنظيف
        cutoff = now - 3600
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    changes.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in changes[:limit]]

def get_rank_from_bitvavo(coin):
    now = time.time()
    scores = []
    for c in list(watchlist):
        dq = prices[c]
        if not dq: 
            continue
        cur = dq[-1][1]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 120:
                old = pr; break
        if old and old > 0:
            ch = ((cur - old)/old*100.0)
            scores.append((c, ch))
    scores.sort(key=lambda x: x[1], reverse=True)
    return {sym:i+1 for i,(sym,_) in enumerate(scores)}.get(coin, 999)

# رتبة مؤقتة إذا خارج الغرفة (منع 999 القاتلة)
def safe_rank(coin):
    rk = get_rank_from_bitvavo(coin)
    if rk != 999:
        return rk
    dq = prices[coin]
    if dq and len(dq) >= 2:
        now = time.time()
        cur = dq[-1][1]
        old = next((pr for ts,pr in reversed(dq) if now-ts>=120), None)
        if old and old>0:
            r120 = (cur-old)/old*100.0
            if r120 >= 2.0:
                return 1
    return 50

# =========================
# 📚 دفتر الأوامر + ميزاته (مع كاش)
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
# 🕯️ شموع + ATR + Vol
# =========================
def get_candles(coin, interval="1m", limit=60):
    now = time.time()
    key = (coin, interval)
    rec = _candles_cache.get(key)
    if rec and now - rec["ts"] < CANDLES_CACHE_SEC:
        return rec["data"]

    resp = http_get(f"{BASE_URL}/{coin}-EUR/candles", {"interval": interval, "limit": limit}, timeout=8)
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
    return sum(trs)/len(trs) if trs else 0.0

def _range_pct(candles, n):
    if len(candles) < n: return 0.0
    hi = max(float(c[2]) for c in candles[-n:])
    lo = min(float(c[3]) for c in candles[-n:])
    mid = (hi+lo)/2.0
    return ((hi-lo)/mid*100.0) if mid>0 else 0.0

# =========================
# 🧨 Exploder Score (+ تفاصيل)
# =========================
def exploder_score(coin):
    """
    يرجّع (score, details)
    details: dict يحتوي r1/r5/r15/accel/vol_spike/vol_expansion/tight/ob feats
    """
    now = time.time()
    dq = prices[coin]
    if not dq:
        return 0.0, {}

    def pct_back(lb):
        old = None; cur = dq[-1][1]
        for ts, pr in reversed(dq):
            if now - ts >= lb:
                old = pr; break
        return ((cur - old)/old*100.0) if (old and old > 0) else 0.0

    r1  = pct_back(60)
    r5  = pct_back(300)
    r15 = pct_back(900)

    # تسارع r1m: r1 الآن – r1 قبل 120s
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

    c1m = get_candles(coin, "1m", 60)
    vol5 = sum(float(c[5]) for c in c1m[-5:]) if len(c1m) >= 5 else 0.0
    vol30 = (sum(float(c[5]) for c in c1m[-30:]) / 30.0) if len(c1m) >= 30 else 0.0
    vol_spike = (vol5 / (vol30*5.0)) if (vol30>0) else 0.0

    atr_short = _atr(c1m[-20:], n=14) if len(c1m) >= 20 else 0.0
    atr_long  = _atr(c1m[-60:], n=14) if len(c1m) >= 60 else (atr_short or 1.0)
    vol_expansion = (atr_short/atr_long) if atr_long > 0 else 0.0

    rng30 = _range_pct(c1m, 30)
    tight_then_break = 1.0 if (rng30 <= 0.8 and r5 >= 1.0) else 0.0

    feats = ob_features(f"{coin}-EUR") or {"spread_bp":9999, "bid_eur":0, "imb":0}
    spread_ok = 1.0 if feats["spread_bp"] <= OB_MAX_SPREAD_BP else 0.0
    liq_ok    = min(1.0, feats["bid_eur"]/OB_MIN_BID_EUR_REF)
    imb_ok    = min(1.0, feats["imb"]/2.0)

    penalty = -10.0 if r15 <= -2.0 else 0.0

    score = (
        22.0 * max(0.0, min(1.5, r1/1.5)) +
        18.0 * max(0.0, min(1.5, accel/1.5)) +
        22.0 * max(0.0, min(2.0, vol_spike)) +
        14.0 * max(0.0, min(2.0, vol_expansion)) +
        10.0 * tight_then_break +
        7.0  * spread_ok +
        5.0  * liq_ok +
        4.0  * imb_ok +
        penalty
    )
    score = max(0.0, min(100.0, score))
    details = {
        "r1": r1, "r5": r5, "r15": r15, "accel": accel, "vol_spike": vol_spike,
        "vol_expansion": vol_expansion, "rng30": rng30,
        "spread_bp": feats["spread_bp"], "bid_eur": feats["bid_eur"], "imb": feats["imb"]
    }
    return score, details

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
        if len(dq) < 2: continue
        old = None; cur = dq[-1][1]
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
    elif h < 0.35: m = 0.90
    elif h < 0.60: m = 1.00
    else:          m = 1.25
    return m

# =========================
# 🧩 الأنماط (Top10/Top1)
# =========================
def check_top10_pattern(coin, m):
    thresh = BASE_STEP_PCT * m
    now = time.time()
    dq = prices[coin]
    if len(dq) < 3: return False
    window = [(ts, p) for ts, p in dq if ts >= now - STEP_WINDOW_SEC]
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
            if ch2 >= thresh:
                return True
            if (pr - last_p) / last_p * 100.0 <= -thresh:
                step1 = False; p0 = pr
    return False

def check_top1_pattern(coin, m):
    seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    seq_parts = [x * m for x in seq_parts]
    now = time.time()
    dq = prices[coin]
    if len(dq) < 3: return False
    window = [(ts, p) for ts, p in dq if ts >= now - SEQ_WINDOW_SEC]
    if len(window) < 3: return False

    slack = 0.3 * m
    base_p = window[0][1]
    step_i = 0
    peak_after = base_p
    for ts, pr in window[1:]:
        ch = (pr - base_p) / base_p * 100.0
        need = seq_parts[step_i]
        if ch >= need:
            step_i += 1
            base_p = pr
            peak_after = pr
            if step_i == len(seq_parts):
                return True
        else:
            drop = (pr - peak_after) / peak_after * 100.0
            if drop <= -(slack):
                base_p = pr; peak_after = pr; step_i = 0
    return False

# =========================
# ⚡️ Pre-Exploder (مبكّر قبل الانفجار)
# =========================
def _pct_back_seconds(dq, sec, now):
    old = None; cur = dq[-1][1] if dq else None
    for ts, pr in reversed(dq):
        if now - ts >= sec:
            old = pr; break
    if not cur or not old or old <= 0: return 0.0
    return (cur - old) / old * 100.0

def pre_exploder_check(coin, details):
    """
    شروط مرنة: نطاق ضيّق + تسارع قصير + حجم/OB محترم
    - r10 ≥ PRE_R10_MIN
    - accel_short = r10 - r30 ≥ PRE_ACCEL_MIN
    - rng30 <= PRE_TIGHT_RANGE_MAX (أو نتجاهله إذا عمر الشموع قصير)
    - vol_spike ≥ PRE_VOLSPIKE_MIN (أو OB جيّد)
    """
    now = time.time()
    dq = prices[coin]
    if len(dq) < 4: return False

    r10 = _pct_back_seconds(dq, 10, now)
    r30 = _pct_back_seconds(dq, 30, now)
    accel_short = r10 - r30

    rng30 = details.get("rng30", 999)
    vol_spike = details.get("vol_spike", 0.0)
    spread_bp = details.get("spread_bp", 9999)
    bid_eur   = details.get("bid_eur", 0.0)
    imb       = details.get("imb", 0.0)

    ob_good = (spread_bp <= OB_MAX_SPREAD_BP and bid_eur >= 0.4*OB_MIN_BID_EUR_REF and imb >= 1.1)
    vol_good = (vol_spike >= PRE_VOLSPIKE_MIN) or ob_good

    c1m = get_candles(coin, "1m", 60)
    short_history = len(c1m) < 30  # إدراج/تاريخ قصير

    return (r10 >= PRE_R10_MIN and accel_short >= PRE_ACCEL_MIN
            and (short_history or rng30 <= PRE_TIGHT_RANGE_MAX)
            and vol_good)

# =========================
# 🧲 مدخل ساخن (التقاط مبكر قبل الأنماط الثقيلة)
# =========================
def hot_entry(coin):
    now = time.time()
    dq = prices[coin]
    if len(dq) < 6: return False
    r60   = _pct_back_seconds(dq, 60, now)
    r120  = _pct_back_seconds(dq, 120, now)
    r300  = _pct_back_seconds(dq, 300, now)
    accel = r60 - r120
    feats = ob_features(f"{coin}-EUR") or {}
    spread_ok = feats.get("spread_bp", 9999) <= OB_MAX_SPREAD_BP
    return (r60 >= 1.2 and r300 >= 3.0 and accel >= 0.5 and spread_ok)

# =========================
# 📣 إرسال الإشعارات (مع Booster + Force)
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

def _notify_core(coin, tag, extra, force=False):
    rk = safe_rank(coin)  # بدّلنا لـ safe_rank
    if (not force) and rk > RANK_FILTER:
        return False
    msg = f"🚀 {coin} {extra} #{tag} #top{rk}"
    send_message(msg)
    if SAQAR_WEBHOOK:
        try:
            payload = {"message": {"text": f"اشتري {coin}"}}
            requests.post(SAQAR_WEBHOOK, json=payload, timeout=5)
        except Exception:
            pass
    return True

def notify_buy(coin, tag, score, extra_note=""):
    now = time.time()
    la_ts = last_alert_ts.get(coin, 0)
    la = last_alert_info.get(coin)

    can_boost = False
    if BOOST_ENABLE and la:
        can_boost = (score >= la.get("score", 0) + BOOST_SCORE_GAP) and ((now - la.get("ts", 0)) >= BOOST_MIN_DELAY_SEC)

    if (now - la_ts < BUY_COOLDOWN_SEC) and not can_boost:
        return

    # تجاوز فلتر #topN بحالات السكّور الصارخ
    force = (score >= TH_TOP1 + 8)
    ok = _notify_core(coin, tag, f"{extra_note} Exploder {score:.0f}", force=force)
    if ok:
        last_alert_ts[coin] = now
        last_alert_info[coin] = {"ts": now, "score": score, "tag": tag}

# =========================
# 🔁 العمال (Room / Analyzer / Price)
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
    """تغذية أسعار شاملة لكل أزواج EUR (مش بس watchlist)."""
    while True:
        now = time.time()
        syms = get_all_eur_symbols()
        refresh_bulk_prices()
        pxmap = dict(_px_cache["map"])
        for s in syms:
            pr = pxmap.get(f"{s}-EUR")
            if pr is None:
                continue
            dq = prices[s]
            dq.append((now, pr))
            cutoff = now - 3600
            while dq and dq[0][0] < cutoff:
                dq.popleft()
        time.sleep(SCAN_INTERVAL)

def analyzer():
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(0.5)
            continue
        try:
            compute_market_heat()
            m = adaptive_multipliers()
            with lock:
                syms = list(watchlist)

            for s in syms:
                dq = prices[s]
                # حارس ركود: تجاهل الرمز إذا آخر سعر قديم
                if not dq or (time.time() - dq[-1][0] > max(WS_STALENESS_SEC, SCAN_INTERVAL * 2)):
                    continue

                # انحياز 24h للأنماط فقط (خفّفنا التشديد)
                d24 = get_daily_change_pct(s)
                m_local = m * (EASE_M_FACTOR if d24 <= DAILY_EASE_MAX_24H else (TIGHT_M_FACTOR if d24 >= DAILY_TIGHT_MIN_24H else 1.0))

                score, det = exploder_score(s)

                # 🔥 مدخل ساخن قبل الأنماط الثقيلة
                if hot_entry(s):
                    notify_buy(s, tag="hot", score=max(score, TH_PRE+5), extra_note="🚨")
                    continue

                # ترتيب الأولوية: top1 > top10 > pre
                if check_top1_pattern(s, m_local) and score >= TH_TOP1:
                    notify_buy(s, tag="top1", score=score, extra_note="🔥")
                    continue

                if check_top10_pattern(s, m_local) and score >= TH_TOP10:
                    notify_buy(s, tag="top10", score=score, extra_note="⚡")
                    continue

                # Pre-Exploder (مبكر)
                if score >= TH_PRE and pre_exploder_check(s, det):
                    notify_buy(s, tag="pre", score=score, extra_note="⏳")
        except Exception as e:
            print("analyzer error:", e)
        time.sleep(ANALYZER_TICK_SEC)

# =========================
# 🌐 Webhook & Health
# =========================
def build_status_text():
    def pct_change_from_lookback(dq, lookback_sec, now_ts):
        if not dq: return 0.0
        cur = dq[-1][1]
        old = None
        for ts, pr in reversed(dq):
            if now_ts - ts >= lookback_sec:
                old = pr; break
        return ((cur - old)/old*100.0) if (old and old>0) else 0.0

    def drawdown_20m(dq, now_ts):
        if not dq: return 0.0
        cur = dq[-1][1]
        mx = max(pr for ts, pr in dq if now_ts - ts <= 1200) if dq else None
        if mx and mx > 0: return (cur - mx)/mx*100.0
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
        rank = safe_rank(c)
        rows.append((c, r1m, r5m, r15m, dd20, rank))
    rows.sort(key=lambda x: x[2], reverse=True)

    lines = [f"📊 غرفة: {len(watchlist)}/{MAX_ROOM} | Heat={heat_ewma:.2f}"]
    if not rows:
        lines.append("— لا بيانات بعد.")
        return "\n".join(lines)
    for i, (c, r1m, r5m, r15m, dd20, rank) in enumerate(rows, 1):
        lines.append(f"{i:02d}. {c} #top{rank} | r1m {r1m:+.2f}% | r5m {r5m:+.2f}% | r15m {r15m:+.2f}% | DD20 {dd20:+.2f}%")
        if i >= 30: break
    return "\n".join(lines)

@app.route("/", methods=["GET"])
def health():
    return "Predictor bot is alive ✅", 200

@app.route("/stats", methods=["GET"])
def stats():
    return {"watchlist": list(watchlist), "heat": round(heat_ewma, 4), "roomsz": len(watchlist)}, 200

@app.route("/debug_stale", methods=["GET"])
def debug_stale():
    now = time.time()
    rows = []
    with lock:
        for c in list(watchlist):
            dq = prices[c]
            age = round(now - dq[-1][0], 2) if dq else None
            rows.append({"coin": c, "last_age_sec": age, "points": len(dq)})
    rows.sort(key=lambda x: (x["last_age_sec"] is None, x["last_age_sec"] or 1e9))
    return {"stale": rows[:50], "heat": round(heat_ewma, 3)}, 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text: return "ok", 200

    if text in {"الحالة", "/status", "/stats", "شو عم تعمل", "/شو_عم_تعمل", "status"}:
        send_message(build_status_text()); return "ok", 200
    return "ok", 200

# =========================
# 🔌 WebSocket (اختياري)
# =========================
_ws_conn = None
_ws_running = False
_ws_lock = Lock()
_ws_subscribed = set()
WS_URL = "wss://ws.bitvavo.com/v2/"

def _ws_sub_payload(markets):
    return {"action": "subscribe", "channels": [{"name": "ticker", "markets": markets}]}

def _ws_unsub_payload(markets):
    return {"action": "unsubscribe", "channels": [{"name": "ticker", "markets": markets}]}

def _ws_on_open(ws):
    try:
        mkts = [f"{c}-EUR" for c in list(watchlist)]
        if mkts:
            ws.send(json.dumps(_ws_sub_payload(mkts)))
    except Exception:
        traceback.print_exc()

def _ws_on_message(ws, message):
    try:
        msg = json.loads(message)
    except Exception:
        return
    if isinstance(msg, dict) and msg.get("event") in ("subscribe", "subscribed"):
        return
    if isinstance(msg, dict) and msg.get("event") == "ticker":
        market = msg.get("market")  # مثل ADA-EUR
        price  = msg.get("price") or msg.get("lastPrice") or msg.get("open")
        if not market or not price:
            return
        try:
            p = float(price)
            base = market.split("-")[0]
            now = time.time()
            with lock:
                dq = prices[base]
                dq.append((now, p))
                cutoff = now - 3600
                while dq and dq[0][0] < cutoff:
                    dq.popleft()
        except Exception:
            pass

def _ws_on_error(ws, err):
    print("WS error:", err)

def _ws_on_close(ws, code, reason):
    global _ws_running
    _ws_running = False
    print("WS closed:", code, reason)

def _ws_thread():
    global _ws_conn, _ws_running
    while True:
        try:
            _ws_running = True
            from websocket import WebSocketApp
            _ws_conn = WebSocketApp(
                WS_URL,
                on_open=_ws_on_open,
                on_message=_ws_on_message,
                on_error=_ws_on_error,
                on_close=_ws_on_close
            )
            _ws_conn.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            print("WS loop exception:", e)
        finally:
            _ws_running = False
            time.sleep(2)

def _ws_manager():
    global _ws_subscribed
    last_sent = set()
    while True:
        try:
            if not _ws_running or not _ws_conn:
                time.sleep(1); continue
            wanted = set(f"{c}-EUR" for c in list(watchlist))
            add = sorted(list(wanted - last_sent))
            rem = sorted(list(last_sent - wanted))
            if add:
                try: _ws_conn.send(json.dumps(_ws_sub_payload(add)))
                except Exception: pass
            if rem:
                try: _ws_conn.send(json.dumps(_ws_unsub_payload(rem)))
                except Exception: pass
            last_sent = wanted
        except Exception as e:
            print("ws_manager error:", e)
        time.sleep(1)

# =========================
# 🚀 التشغيل
# =========================
_started = #False
#def start_workers_once():
    global _started
    if _started: return
    Thread(target=room_refresher, daemon=True).start()
    Thread(target=price_poller,   daemon=True).start()
    Thread(target=analyzer,       daemon=True).start()
    if USE_WS:
        Thread(target=_ws_thread,  daemon=True).start()
        Thread(target=_ws_manager, daemon=True).start()
    _started = True

start_workers_once()

@app.before_request
def _ensure_started():
    start_workers_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))