# -*- coding: utf-8 -*-
import os, time, json, requests, redis
from flask import Flask, request
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from collections import deque, defaultdict
from datetime import datetime

# ============================================================
# 🔧 إعدادات قابلة للتعديل (كل ما يخص السلوك هنا)
# ============================================================
# عام
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 90))    # كل كم ثانية نعيد تجميع/تصنيف السوق
SCAN_INTERVAL_SEC    = int(os.getenv("SCAN_INTERVAL_SEC", 5))      # دورة المراقبة الحية
MAX_ROOM             = int(os.getenv("MAX_ROOM", 30))              # أقصى عدد عملات في الغرفة
THREADS              = int(os.getenv("THREADS", 32))

# سيولة/مهلات الشبكة
MIN_24H_EUR          = float(os.getenv("MIN_24H_EUR", 15000))      # أدنى سيولة يومية لقبول السوق
VOL_CACHE_TTL        = int(os.getenv("VOL_CACHE_TTL", 120))        # كاش سيولة 24h (ثواني)
CANDLE_TIMEOUT       = int(os.getenv("CANDLE_TIMEOUT", 10))
TICKER_TIMEOUT       = int(os.getenv("TICKER_TIMEOUT", 6))

# نقاط وترتيب
WEIGHTS              = {"5m": 0.4, "15m": 0.3, "30m": 0.2, "1h": 0.1}
RANK_POINTS          = [5, 4, 3, 2, 1]                             # مكافأة مراكز 1..5
RANK_TOP             = {"5m": 20, "15m": 10, "30m": 10, "1h": 8}   # كم ناخذ من كل فريم قبل التجميع
TOP10_PRIORITY_BONUS = float(os.getenv("TOP10_PRIORITY_BONUS", 1)) # بونص يحافظ على وجود Top10 أعلى الغرفة

# تدهور نقاط لمن يفقد الزخم (ينزل/يخرج تلقائيًا)
DECAY_EVERY_SEC      = int(os.getenv("DECAY_EVERY_SEC", 2*BATCH_INTERVAL_SEC))
DECAY_VALUE          = float(os.getenv("DECAY_VALUE", 0.6))

# استبدال الأضعف إن امتلأت الغرفة
REPLACE_MARGIN       = float(os.getenv("REPLACE_MARGIN", 0.0))     # سماحية فرق نقاط لاستبدال الأضعف

# ------------------------------
# 🔔 سياسة إشعارات الشراء (Top10 فقط)
# ------------------------------
ALERTS_TOP10_ONLY        = int(os.getenv("ALERTS_TOP10_ONLY", 1))  # لا إشعار إلا إذا كان rank5<=10
STARTUP_MUTE_SEC         = int(os.getenv("STARTUP_MUTE_SEC", 120)) # صمت أول التشغيل
ENTRY_MUTE_SEC           = int(os.getenv("ENTRY_MUTE_SEC", 90))    # صمت بعد دخول العملة للغرفة
MIN_SCANS_IN_ROOM        = int(os.getenv("MIN_SCANS_IN_ROOM", 2))  # على الأقل دورتين مراقبة
GLOBAL_ALERT_COOLDOWN    = int(os.getenv("GLOBAL_ALERT_COOLDOWN", 20)) # حد أدنى بين أي إشعارين
CONFIRM_WINDOW_SEC       = int(os.getenv("CONFIRM_WINDOW_SEC", 15)) # تأكيد مرحلتين
MIN_MOVE_FROM_ENTRY      = float(os.getenv("MIN_MOVE_FROM_ENTRY", 0.4))# % من سعر دخول الغرفة
MIN_CH5_FOR_ALERT        = float(os.getenv("MIN_CH5_FOR_ALERT", 1.2))   # حد أدنى 5m وقت الإرسال
MIN_SPIKE_FOR_ALERT      = float(os.getenv("MIN_SPIKE_FOR_ALERT", 1.3)) # حد أدنى سبايك وقت الإرسال
REARM_PCT                = float(os.getenv("REARM_PCT", 1.5))           # إعادة تسليح بعد +1.5%
COOLDOWN_SEC             = int(os.getenv("COOLDOWN_SEC", 300))          # تبريد إشعار للعملة

# منطق الاختراق/القفزة داخل المراقبة
BREAKOUT_30M_PCT         = float(os.getenv("BREAKOUT_30M_PCT", 0.8))    # % فوق high30
ROOM_TTL_SEC             = int(os.getenv("ROOM_TTL_SEC", 3*3600))

# مفاتيح التشغيل (تلغرام/ويبهوك)
BOT_TOKEN     = os.getenv("BOT_TOKEN");   CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL     = os.getenv("REDIS_URL");   SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")
# ============================================================


# ============== تهيئة أساسية ==============
app  = Flask(__name__)
r    = redis.from_url(REDIS_URL) if REDIS_URL else None
sess = requests.Session()
START_TS = time.time()
lock = Lock()

# مفاتيح Redis
NS                = os.getenv("REDIS_NS", "room")
KEY_WATCH_SET     = f"{NS}:watch"
KEY_COIN_HASH     = lambda s: f"{NS}:coin:{s}"
KEY_COOLDOWN      = lambda s: f"{NS}:cool:{s}"
KEY_MARKETS_CACHE = f"{NS}:markets"
KEY_24H_CACHE     = f"{NS}:24h"
KEY_SEQ           = f"{NS}:seq"
KEY_GLOBAL_ALERT_TS = f"{NS}:global:last_alert_ts"
KEY_PENDING       = lambda s: f"{NS}:pending:{s}"
KEY_REASON        = lambda s: f"{NS}:reason:{s}"
KEY_SCAN_COUNT    = lambda s: f"{NS}:scans:{s}"

# كاش داخلي
price_hist    = defaultdict(lambda: deque(maxlen=360))   # كل 5 ثواني ≈ 30 دقيقة
metrics_cache = {}                                       # sym -> آخر مقاييس
_bg_started   = False


# ============== مراسلة ==============
def tg(msg: str):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        sess.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  data={"chat_id": CHAT_ID, "text": msg}, timeout=8)
    except Exception as e: print("TG error:", e)

def notify_saqr(sym: str):
    if not SAQAR_WEBHOOK: return
    try:
        payload = {"message": {"text": f"اشتري {sym.upper()}"}}
        resp = sess.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        print("→ صقر:", resp.status_code, resp.text[:160])
    except Exception as e: print("Saqr error:", e)


# ============== Bitvavo helpers ==============
def get_markets_eur():
    try:
        res = sess.get("https://api.bitvavo.com/v2/markets", timeout=CANDLE_TIMEOUT).json()
        return [m["market"] for m in res if m.get("market","").endswith("-EUR")]
    except Exception as e: print("markets error:", e); return []

def get_candles_1m(market: str, limit: int = 60):
    try:
        return sess.get(
            f"https://api.bitvavo.com/v2/{market}/candles?interval=1m&limit={limit}",
            timeout=CANDLE_TIMEOUT
        ).json()
    except Exception as e: print("candles error:", market, e); return []

def get_ticker_price(market: str):
    try:
        data = sess.get(f"https://api.bitvavo.com/v2/ticker/price?market={market}",
                        timeout=TICKER_TIMEOUT).json()
        return float(data.get("price", 0) or 0.0)
    except Exception: return 0.0

def get_24h_stats_eur():
    try:
        arr = sess.get("https://api.bitvavo.com/v2/ticker/24h", timeout=10).json()
        out = {}
        for row in arr:
            m = row.get("market")
            if not m or not m.endswith("-EUR"): continue
            vol_eur = float(row.get("volume", 0)) * float(row.get("last", 0))
            out[m] = vol_eur
        return out
    except Exception: return {}

# ============== حسابات من 1m ==============
def pct(a, b): return ((a-b)/b*100.0) if b > 0 else 0.0

def changes_from_1m(c):
    if not isinstance(c, list) or len(c) < 2: return None
    closes = [float(x[4]) for x in c]
    vols   = [float(x[5]) for x in c]
    n = len(c)
    def safe(idx): return pct(closes[-1], closes[-idx]) if n >= idx and closes[-idx] > 0 else 0.0
    close  = closes[-1]
    ch_1m, ch_5m, ch_15m, ch_30m, ch_1h = safe(2), safe(6), safe(16), safe(31), safe(60)
    window = min(31, n)
    high30 = max(closes[-window:-1]) if n > 1 else close
    k = min(15, max(1, n-1))
    base = sum(vols[-(k+1):-1]) / k if n >= 3 else 0.0
    spike = (vols[-1]/base) if base > 0 else 1.0
    return {"close": close, "ch_1m": ch_1m, "ch_5m": ch_5m, "ch_15m": ch_15m,
            "ch_30m": ch_30m, "ch_1h": ch_1h, "spike": spike, "high30": high30}


# ============== إدارة الغرفة ==============
def room_add(sym, entry_price, pts_add=0, ranks_str=""):
    hkey = KEY_COIN_HASH(sym)
    now  = int(time.time())
    if r.exists(hkey):
        cur = float(r.hget(hkey, "pts") or b"0")
        p = r.pipeline()
        p.hset(hkey, mapping={
            "pts": f"{cur + float(pts_add):.4f}",
            "last_pts_add": f"{float(pts_add):.4f}",
            "last_pts_ts": str(now),
            "ranks": ranks_str
        })
        p.expire(hkey, ROOM_TTL_SEC)
        p.sadd(KEY_WATCH_SET, sym)
        p.execute()
        return
    seq = r.incr(KEY_SEQ)
    p = r.pipeline()
    p.hset(hkey, mapping={
        "entry_price": f"{entry_price:.12f}",
        "entry_ts": str(now),
        "high": f"{entry_price:.12f}",
        "pts": f"{float(pts_add):.4f}",
        "seq": str(seq),
        "last_pts_add": f"{float(pts_add):.4f}",
        "last_pts_ts": str(now),
        "ranks": ranks_str,
        # نخزن آخر رتبة 5m وتاريخها لتصفية الإشعارات Top10
        "rank5": "-1",
        "rank_ts": "0"
    })
    p.expire(hkey, ROOM_TTL_SEC)
    p.sadd(KEY_WATCH_SET, sym)
    p.execute()

def room_get(sym):
    d = r.hgetall(KEY_COIN_HASH(sym))
    if not d: return None
    g = lambda k, dv="0": (d.get(k, dv.encode()).decode() if isinstance(d.get(k), (bytes, bytearray)) else dv)
    try:
        return {
            "entry_price": float(g(b"entry_price")),
            "entry_ts": int(g(b"entry_ts")),
            "high": float(g(b"high")),
            "pts": float(g(b"pts")),
            "seq": int(g(b"seq")),
            "last_pts_add": float(g(b"last_pts_add")),
            "last_pts_ts": int(g(b"last_pts_ts")),
            "ranks": g(b"ranks", ""),
            "rank5": int(g(b"rank5", "-1")),
            "rank_ts": int(g(b"rank_ts", "0")),
            "last_alert_ts": int(g(b"last_alert_ts", "0")),
            "last_alert_price": float(g(b"last_alert_price", "0")),
            "last_alert_reason": g(b"last_alert_reason", "")
        }
    except Exception:
        return None

def room_update_high(sym, v): r.hset(KEY_COIN_HASH(sym), "high", f"{v:.12f}")
def in_cooldown(sym): return bool(r.get(KEY_COOLDOWN(sym)))
def mark_cooldown(sym): r.setex(KEY_COOLDOWN(sym), COOLDOWN_SEC, 1)

def set_last_alert(sym, ts, price, reason):
    r.hset(KEY_COIN_HASH(sym), mapping={
        "last_alert_ts": str(ts),
        "last_alert_price": f"{price:.12f}",
        "last_alert_reason": reason
    })

def room_members():
    syms = list(r.smembers(KEY_WATCH_SET))
    out = []
    for b in syms:
        s = b.decode()
        if r.exists(KEY_COIN_HASH(s)): out.append(s)
        else: r.srem(KEY_WATCH_SET, s)
    return out

def room_size(): return r.scard(KEY_WATCH_SET)

def weakest_member():
    rows = []
    now = int(time.time())
    for s in room_members():
        d = r.hgetall(KEY_COIN_HASH(s))
        pts = float(d.get(b"pts", b"0") or 0)
        fresh = now - int(d.get(b"last_pts_ts", b"0") or 0)
        rows.append((s, pts, fresh))
    rows.sort(key=lambda x: (x[1], -x[2]))  # أقل نقاط ثم الأقدم
    return rows[0] if rows else (None, 0.0, 0)

def try_admit(sym, entry_price, score, reason=""):
    if room_size() < MAX_ROOM:
        room_add(sym, entry_price, pts_add=score, ranks_str=reason); return True
    wsym, wpts, _ = weakest_member()
    force = reason in ("top10", "top1_seed")  # ندفع Top10/Top1 بالقوة
    if wsym and (force or score >= wpts + REPLACE_MARGIN):
        r.delete(KEY_COIN_HASH(wsym)); r.srem(KEY_WATCH_SET, wsym)
        room_add(sym, entry_price, pts_add=score, ranks_str=reason); return True
    return False

def decay_room():
    now = int(time.time())
    for s in room_members():
        d = r.hgetall(KEY_COIN_HASH(s))
        last = int(d.get(b"last_pts_ts", b"0") or 0)
        pts  = float(d.get(b"pts", b"0") or 0)
        if now - last >= DECAY_EVERY_SEC and pts > 0:
            r.hincrbyfloat(KEY_COIN_HASH(s), "pts", -DECAY_VALUE)


# ============== لقطة حيّة ==============
def fresh_snapshot(sym):
    mkt = f"{sym}-EUR"
    c = get_candles_1m(mkt, limit=31)
    d = changes_from_1m(c) if c else None
    px = get_ticker_price(mkt)
    if d:
        if px <= 0: px = d["close"]
        return {"price": px, "ch5": d["ch_5m"], "spike": d["spike"], "high30": d["high30"], "close": d["close"]}
    return {"price": px or 0.0, "ch5": 0.0, "spike": 1.0, "high30": 0.0, "close": 0.0}


# ============== دفعة التجميع: نحافظ على Top10 ديناميكيًا ==============
def batch_collect():
    try:
        # 1) أسواق + سيولة
        markets_b = r.get(KEY_MARKETS_CACHE)
        markets = json.loads(markets_b.decode() if isinstance(markets_b,(bytes,bytearray)) else markets_b) \
                  if markets_b else get_markets_eur()
        if not markets_b: r.setex(KEY_MARKETS_CACHE, 3600, json.dumps(markets))

        vol24_b = r.get(KEY_24H_CACHE)
        vol24 = json.loads(vol24_b.decode() if isinstance(vol24_b,(bytes,bytearray)) else vol24_b) \
                if vol24_b else get_24h_stats_eur()
        if not vol24_b: r.setex(KEY_24H_CACHE, VOL_CACHE_TTL, json.dumps(vol24))
        vol_filter_active = bool(vol24)

        # 2) قراءة المقاييس لكل سوق
        def fetch_one(market):
            c = get_candles_1m(market, limit=60)
            d = changes_from_1m(c)
            if not d: return None
            if vol_filter_active and vol24.get(market, 0.0) < MIN_24H_EUR and d["ch_5m"] < 2.0:
                return None
            return market, d

        rows = []
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            for res in ex.map(fetch_one, markets):
                if res: rows.append(res)
        if not rows: return

        # 3) ترتيب لكل فريم + أخذ TopN
        ranks = {"5m":[], "15m":[], "30m":[], "1h":[]}
        for market, d in rows:
            ranks["5m"].append((market, d["ch_5m"], d))
            ranks["15m"].append((market, d["ch_15m"], d))
            ranks["30m"].append((market, d["ch_30m"], d))
            ranks["1h"].append((market, d["ch_1h"], d))
        for k in ranks:
            ranks[k].sort(key=lambda x: x[1], reverse=True)
            ranks[k] = ranks[k][:RANK_TOP.get(k, 5)]

        # 4) حساب نقاط + حفظ rank5 الحالي في العملة
        score = defaultdict(float)
        best_refs = {}
        rank_map  = defaultdict(dict)
        nowi = int(time.time())

        for idx,(market,_,d) in enumerate(ranks["5m"]):
            sym = market.replace("-EUR","")
            p = {"rank5": idx+1, "rank_ts": nowi}
            r.hset(KEY_COIN_HASH(sym), mapping={k:str(v) for k,v in p.items()})
        for tf in ["5m","15m","30m","1h"]:
            for idx,(market,_,d) in enumerate(ranks[tf]):
                sym = market.replace("-EUR","")
                w   = WEIGHTS[tf]
                pts = RANK_POINTS[idx] if idx < len(RANK_POINTS) else 1
                if tf == "5m" and idx < 10: pts += TOP10_PRIORITY_BONUS  # بونص Top10
                score[sym] += w * pts
                best_refs.setdefault(sym, (market, d))
                rank_map[sym][tf] = idx + 1

        # 5) ادخال Top10 دائمًا (لكن ديناميكي — بدون تثبيت أبدي)
        top10_syms = [m.replace("-EUR","") for m,_,_ in ranks["5m"][:10]]
        for sym in top10_syms:
            mkt, d = best_refs.get(sym, (f"{sym}-EUR", {"close": get_ticker_price(f"{sym}-EUR") or 0}))
            px = get_ticker_price(mkt) or d["close"]
            if px > 0:
                try_admit(sym, px, score.get(sym, 0), "top10")

        # 6) أكمل حتى 30 بحسب السكور العام
        rest = [(sym,sc) for sym,sc in score.items()]
        rest.sort(key=lambda item: (-item[1],))
        for sym,sc in rest:
            if room_size() >= MAX_ROOM: break
            mkt, d = best_refs[sym]
            px = get_ticker_price(mkt) or d["close"]
            if px > 0:
                try_admit(sym, px, sc, "score")

        # 7) تدهور نقاط من لا يُحدَّث ليرتّب نزول/خروج طبيعي
        decay_room()

        tg(f"✅ Top10 mode: غرفة {room_size()}/{MAX_ROOM} | تم تحديث Top10 وإعادة ترتيب المرشحين.")
    except Exception as e:
        print("batch_collect error:", e)


def batch_loop():
    while True:
        t0 = time.time()
        batch_collect()
        time.sleep(max(5.0, BATCH_INTERVAL_SEC - (time.time()-t0)))


# ============== مراقبة حية + إشعار شراء (Top10 فقط) ==============
def ensure_metrics(sym):
    now = time.time()
    info = metrics_cache.get(sym)
    if not info or now - info["ts"] >= 60:
        mkt = f"{sym}-EUR"
        c = get_candles_1m(mkt, limit=31)
        d = changes_from_1m(c) if c else None
        if d:
            metrics_cache[sym] = {"ts": now, "ch5": d["ch_5m"], "spike": d["spike"],
                                  "close": d["close"], "high30": d["high30"]}
        else:
            p = get_ticker_price(mkt)
            metrics_cache[sym] = {"ts": now, "ch5": 0.0, "spike": 1.0, "close": p, "high30": p}

def monitor_room():
    while True:
        try:
            syms = room_members()
            now  = time.time()
            for sym in syms:
                mkt = f"{sym}-EUR"
                ensure_metrics(sym)
                mc = metrics_cache.get(sym, {"ch5":0.0,"spike":1.0,"close":0.0,"high30":0.0})
                price = get_ticker_price(mkt) or mc["close"]
                price_hist[sym].append((now, price))

                st = room_get(sym)
                if not st: continue
                entry_price, entry_ts, high_stored, pts = st["entry_price"], st["entry_ts"], st["high"], st["pts"]
                rank5, rank_ts = st["rank5"], st["rank_ts"]

                # تحديث high
                if price > high_stored:
                    room_update_high(sym, price); high_stored = price

                # عدّاد لفّات داخل الغرفة
                r.incr(KEY_SCAN_COUNT(sym))

                # شروط عامة للإشعار (داخل الغرفة فقط)
                if time.time() - START_TS < STARTUP_MUTE_SEC:       # صمت بدء التشغيل
                    continue
                if (time.time() - entry_ts) < ENTRY_MUTE_SEC:        # صمت دخول الغرفة
                    continue
                if int(r.get(KEY_SCAN_COUNT(sym) or 0)) < MIN_SCANS_IN_ROOM:
                    continue
                if ALERTS_TOP10_ONLY:
                    fresh = (time.time() - rank_ts) <= (2*BATCH_INTERVAL_SEC + 15)
                    if not (rank5 > 0 and rank5 <= 10 and fresh):
                        continue  # إشعار فقط لمن هو داخل Top10 (5m) حديثاً

                # لقطة حيّة قبل الإرسال
                snap = fresh_snapshot(sym)
                price = snap["price"]; mc["ch5"] = snap["ch5"]; mc["spike"] = snap["spike"]; mc["high30"] = snap["high30"]

                # تأكيد حركة/زخم داخلي
                move_from_entry = pct(price, entry_price)
                cond_ch5   = (mc["ch5"]   >= MIN_CH5_FOR_ALERT)
                cond_spike = (mc["spike"] >= MIN_SPIKE_FOR_ALERT)
                cond_break = (mc["high30"] > 0 and pct(price, mc["high30"]) >= BREAKOUT_30M_PCT)
                reason = "قفزة5م+سبايك" if (cond_ch5 and cond_spike) else ("كسر30د" if cond_break else "")

                if not reason:  # ما في سبب معتبر
                    continue
                if move_from_entry < MIN_MOVE_FROM_ENTRY:  # لازم حركة فعلية بعد دخول الغرفة
                    continue

                # قفل عالمي لمنع الضجيج
                last_global = float(r.get(KEY_GLOBAL_ALERT_TS) or 0)
                if time.time() - last_global < GLOBAL_ALERT_COOLDOWN:
                    continue

                # تأكيد مرحلتين (نفس السبب ضمن مهلة)
                pend_ts = float(r.get(KEY_PENDING(sym)) or 0)
                if pend_ts == 0:
                    r.setex(KEY_PENDING(sym), CONFIRM_WINDOW_SEC, time.time())
                    r.setex(KEY_REASON(sym),  CONFIRM_WINDOW_SEC, reason)
                    continue
                else:
                    prev_reason = (r.get(KEY_REASON(sym)) or b"").decode()
                    if prev_reason != reason:
                        r.setex(KEY_PENDING(sym), CONFIRM_WINDOW_SEC, time.time())
                        r.setex(KEY_REASON(sym),  CONFIRM_WINDOW_SEC, reason)
                        continue

                # إعادة تسليح (لا نكرر إلا بعد ارتفاع جديد واضح أو مر الوقت)
                la_ts, la_price, la_reason = st["last_alert_ts"], st["last_alert_price"], st["last_alert_reason"]
                ok_time = (time.time() - la_ts) >= COOLDOWN_SEC
                ok_move = (la_price == 0) or (price >= la_price * (1 + REARM_PCT/100.0))
                if not (ok_time or ok_move):
                    continue

                # أرسل
                entered_at = datetime.fromtimestamp(entry_ts).strftime("%H:%M")
                msg = (f"🚀 {sym} / {int(round(pts))} نقاط | {reason} | منذ الدخول {move_from_entry:+.2f}% | "
                       f"دخل {entered_at} | spikex{mc['spike']:.1f} | 5m {mc['ch5']:+.2f}% | rank5 #{rank5}")
                tg(msg); notify_saqr(sym)
                mark_cooldown(sym)
                set_last_alert(sym, int(time.time()), price, reason)
                r.set(KEY_GLOBAL_ALERT_TS, time.time())
                r.delete(KEY_PENDING(sym)); r.delete(KEY_REASON(sym))

            time.sleep(SCAN_INTERVAL_SEC)
        except Exception as e:
            print("monitor_room error:", e)
            time.sleep(SCAN_INTERVAL_SEC)


# ============== HTTP + أوامر ==============
@app.route("/", methods=["GET"])
def alive():
    return "Top10 Room bot (strict) is alive ✅", 200

def _do_reset(full=False):
    syms = list(r.smembers(KEY_WATCH_SET))
    for b in syms:
        s = b.decode()
        r.delete(KEY_COIN_HASH(s)); r.delete(KEY_COOLDOWN(s)); r.srem(KEY_WATCH_SET, s)
        r.delete(KEY_SCAN_COUNT(s)); r.delete(KEY_PENDING(s)); r.delete(KEY_REASON(s))
    if full:
        r.delete(KEY_MARKETS_CACHE); r.delete(KEY_24H_CACHE); r.delete(KEY_SEQ); r.delete(KEY_GLOBAL_ALERT_TS)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        txt = (data.get("message", {}).get("text") or "").strip().lower()
        if txt in ("ابدأ","start"):
            start_background(); tg("✅ تم تشغيل غرفة عمليات Top10 (strict).")
        elif txt in ("السجل","log"):
            syms = room_members()
            rows = []
            now = int(time.time())
            for s in syms:
                d = r.hgetall(KEY_COIN_HASH(s))
                pts = float(d.get(b"pts", b"0").decode() or "0")
                seq = int(d.get(b"seq", b"0").decode() or "0")
                last_add = float(d.get(b"last_pts_add", b"0").decode() or "0")
                last_ts  = int(d.get(b"last_pts_ts", b"0").decode() or "0")
                ranks    = d.get(b"ranks", b"").decode() if b"ranks" in d else ""
                rank5    = int(d.get(b"rank5", b"-1").decode() or "-1")
                recent = (now - last_ts) <= (BATCH_INTERVAL_SEC + 120)
                rows.append((s, pts, seq, last_add, recent, ranks, rank5))
            rows.sort(key=lambda x: (-1 if (1 <= x[6] <= 10) else 0, x[1]), reverse=True)  # ضع Top10 أعلى
            lines = [f"📊 غرفة {len(rows)}/{MAX_ROOM} (Top10 أعلى القائمة):"]
            for i,(s,pts,seq,last_add,recent,ranks,rank5) in enumerate(rows, start=1):
                flag = " 🆕" if recent and last_add > 0 else ""
                delta = f" +{int(round(last_add))}" if last_add > 0 else ""
                tag   = f"#top{rank5}" if 1 <= rank5 <= 10 else ""
                ranks_str = f"[{ranks}]" if ranks else ""
                lines.append(f"{i}. {s} / {int(round(pts))} نقاط {tag} {ranks_str} [#{seq}{delta}]{flag}")
            tg("\n".join(lines))
        elif txt in ("مسح","reset"):
            _do_reset(full=True); tg("🧹 تم مسح الغرفة والكاش.")
        return "ok", 200
    except Exception as e:
        print("webhook error:", e); return "ok", 200


# ============== تشغيل الخلفيات ==============
def start_background():
    global _bg_started
    if _bg_started: return
    _bg_started = True
    Thread(target=batch_loop, daemon=True).start()
    Thread(target=monitor_room, daemon=True).start()
    print("Background loops started (Top10 strict).")

# ابدأ تلقائيًا
if os.getenv("DISABLE_AUTO_START", "0") != "1":
    start_background()

# محليًا (Railway يمرر PORT عبر gunicorn)
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)