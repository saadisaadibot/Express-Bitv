# -*- coding: utf-8 -*-
import os, time, json, requests, redis
from flask import Flask, request
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from collections import deque, defaultdict
from datetime import datetime

# =========================
# إعدادات
# =========================
BATCH_INTERVAL_SEC = int(os.getenv("BATCH_INTERVAL_SEC", 900))   # 15 دقيقة
ROOM_TTL_SEC       = int(os.getenv("ROOM_TTL_SEC", 3*3600))      # 3 ساعات
TOP_PER_TF         = int(os.getenv("TOP_PER_TF", 20))            # Top لكل فريم قبل الدمج
TOP_MERGED         = int(os.getenv("TOP_MERGED", 20))            # Top النهائي للغرفة
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", 5))      # مراقبة كل 5 ثواني
CANDLE_TIMEOUT     = int(os.getenv("CANDLE_TIMEOUT", 10))
TICKER_TIMEOUT     = int(os.getenv("TICKER_TIMEOUT", 6))
THREADS            = int(os.getenv("THREADS", 32))
MIN_24H_EUR        = float(os.getenv("MIN_24H_EUR", 10000))      # حد سيولة يومية
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC", 300))         # تبريد 5 دقائق/عملة
SPIKE_WEAK         = float(os.getenv("SPIKE_WEAK", 1.3))
JUMP_5M_PCT        = float(os.getenv("JUMP_5M_PCT", 1.5))        # قفزة 5م
BREAKOUT_30M_PCT   = float(os.getenv("BREAKOUT_30M_PCT", 0.8))   # كسر قمة 30د

# مفاتيح
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHAT_ID       = os.getenv("CHAT_ID")
REDIS_URL     = os.getenv("REDIS_URL")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")

# Flask + Redis
app  = Flask(__name__)
r    = redis.from_url(REDIS_URL) if REDIS_URL else None
sess = requests.Session()
lock = Lock()
_bg_started = False

# مفاتيح Redis
NS                = os.getenv("REDIS_NS", "room")
KEY_WATCH_SET     = f"{NS}:watch"
KEY_COIN_HASH     = lambda s: f"{NS}:coin:{s}"
KEY_COOLDOWN      = lambda s: f"{NS}:cool:{s}"
KEY_MARKETS_CACHE = f"{NS}:markets"
KEY_24H_CACHE     = f"{NS}:24h"

# هياكل داخلية
price_hist    = defaultdict(lambda: deque(maxlen=360))  # (ts, price) كل 5ث ≈ 30د
metrics_cache = {}  # sym -> {"ts":..., "ch5":..., "spike":..., "close":..., "high30":...}

# مراسلة
def tg(msg: str):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        sess.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  data={"chat_id": CHAT_ID, "text": msg}, timeout=8)
    except Exception as e:
        print("TG error:", e)

def notify_saqr(sym: str):
    if not SAQAR_WEBHOOK: return
    try:
        payload = {"message": {"text": f"اشتري {sym.upper()}"}}
        resp = sess.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        print("→ صقر:", resp.status_code, resp.text[:160])
    except Exception as e:
        print("Saqr error:", e)

# Bitvavo helpers
def get_markets_eur():
    try:
        res = sess.get("https://api.bitvavo.com/v2/markets", timeout=CANDLE_TIMEOUT).json()
        return [m["market"] for m in res if m.get("market","").endswith("-EUR")]
    except Exception as e:
        print("markets error:", e); return []

def get_candles_1m(market: str, limit: int = 60):
    try:
        return sess.get(
            f"https://api.bitvavo.com/v2/{market}/candles?interval=1m&limit={limit}",
            timeout=CANDLE_TIMEOUT
        ).json()
    except Exception as e:
        print("candles error:", market, e); return []

def get_ticker_price(market: str):
    try:
        data = sess.get(f"https://api.bitvavo.com/v2/ticker/price?market={market}",
                        timeout=TICKER_TIMEOUT).json()
        return float(data.get("price", 0) or 0.0)
    except Exception:
        return 0.0

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
    except Exception:
        return {}

# حسابات التغير من شموع 1m (مرنة)
def pct(a, b): return ((a-b)/b*100.0) if b > 0 else 0.0

def changes_from_1m(c):
    if not isinstance(c, list) or len(c) < 6: return None
    closes = [float(x[4]) for x in c]
    vols   = [float(x[5]) for x in c]
    n = len(c)
    def safe(idx): return pct(closes[-1], closes[-idx]) if n >= idx and closes[-idx] > 0 else 0.0
    close  = closes[-1]
    ch_1m, ch_5m, ch_15m, ch_30m, ch_1h = safe(2), safe(6), safe(16), safe(31), safe(60)
    k = min(15, max(1, n-1))
    base = sum(vols[-(k+1):-1]) / k if n >= 3 else 0.0
    spike = (vols[-1]/base) if base > 0 else 1.0
    high30 = max(closes[-min(31,n):]) if n else close
    return {"close": close, "ch_1m": ch_1m, "ch_5m": ch_5m, "ch_15m": ch_15m,
            "ch_30m": ch_30m, "ch_1h": ch_1h, "spike": spike, "high30": high30}

# إدارة الغرفة
def room_add(sym, entry_price):
    hkey = KEY_COIN_HASH(sym)
    now  = int(time.time())
    p = r.pipeline()
    p.hset(hkey, mapping={"entry_price": f"{entry_price:.12f}",
                          "entry_ts": str(now),
                          "high": f"{entry_price:.12f}"})
    p.expire(hkey, ROOM_TTL_SEC)
    p.sadd(KEY_WATCH_SET, sym)
    p.execute()

def room_get(sym):
    data = r.hgetall(KEY_COIN_HASH(sym))
    if not data: return None
    try:
        return {"entry_price": float(data[b"entry_price"].decode()),
                "entry_ts": int(data[b"entry_ts"].decode()),
                "high": float(data[b"high"].decode())}
    except Exception:
        return None

def room_update_high(sym, v): r.hset(KEY_COIN_HASH(sym), "high", f"{v:.12f}")
def in_cooldown(sym): return bool(r.get(KEY_COOLDOWN(sym)))
def mark_cooldown(sym): r.setex(KEY_COOLDOWN(sym), COOLDOWN_SEC, 1)

def room_members():
    syms = list(r.smembers(KEY_WATCH_SET))
    out = []
    for b in syms:
        s = b.decode()
        if r.exists(KEY_COIN_HASH(s)): out.append(s)
        else: r.srem(KEY_WATCH_SET, s)
    return out

# ----- دمج Top من الفريمات (تصويت + ترتيب) -----
def merge_tops(rank_maps, top_per_tf=TOP_PER_TF, top_final=TOP_MERGED):
    # rank_maps: dict {"5m": [(market, chg), ... رتّب desc], "15m": [...], ...}
    scores = defaultdict(lambda: {"votes":0, "rank_sum":0.0})
    for tf, arr in rank_maps.items():
        for rank, (mkt, ch) in enumerate(arr[:top_per_tf], start=1):
            sym = mkt.replace("-EUR","")
            scores[sym]["votes"] += 1
            # نعطي نقاط أعلى للبداية: 1/الترتيب + المكسب
            scores[sym]["rank_sum"] += (1.0/rank) + max(0.0, ch)/100.0
    # ترتيب نهائي: أولاً عدد الأصوات، ثم rank_sum
    merged = sorted(scores.items(), key=lambda kv: (kv[1]["votes"], kv[1]["rank_sum"]), reverse=True)
    return [sym for sym,_ in merged[:top_final]]

# ----- دفعة التجميع كل 15 دقيقة -----
def batch_collect():
    try:
        markets = r.get(KEY_MARKETS_CACHE)
        if markets: markets = json.loads(markets)
        else:
            markets = get_markets_eur()
            r.setex(KEY_MARKETS_CACHE, 3600, json.dumps(markets))

        vol24 = r.get(KEY_24H_CACHE)
        if vol24: vol24 = json.loads(vol24)
        else:
            vol24 = get_24h_stats_eur()
            r.setex(KEY_24H_CACHE, 300, json.dumps(vol24))

        def fetch_one(market):
            c = get_candles_1m(market, limit=60)
            d = changes_from_1m(c)
            if not d: return None
            if vol24.get(market, 0.0) < MIN_24H_EUR: return None
            return market, d

        rows = []
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            for res in ex.map(fetch_one, markets):
                if res: rows.append(res)
        if not rows:
            print("batch_collect: no rows"); return

        # كون رتب لكل فريم حسب التغير
        rank_maps = {"5m":[], "15m":[], "30m":[], "1h":[]}
        for market, d in rows:
            rank_maps["5m"].append((market, d["ch_5m"]))
            rank_maps["15m"].append((market, d["ch_15m"]))
            rank_maps["30m"].append((market, d["ch_30m"]))
            rank_maps["1h"].append((market, d["ch_1h"]))
        for k in rank_maps:
            rank_maps[k].sort(key=lambda x: x[1], reverse=True)

        merged_syms = merge_tops(rank_maps, top_per_tf=TOP_PER_TF, top_final=TOP_MERGED)

        # أدخل للغرفة
        for sym in merged_syms:
            mkt = f"{sym}-EUR"
            price_now = get_ticker_price(mkt)
            if price_now <= 0:
                # احتياطي من الشموع
                c = get_candles_1m(mkt, limit=2)
                if isinstance(c, list) and len(c) >= 1:
                    price_now = float(c[-1][4])
            if price_now > 0:
                room_add(sym, price_now)

        tg(f"✅ تحديث غرفة المرشحين: {len(merged_syms)} عملة | دمج 5m/15m/30m/1h | سيولة≥€{int(MIN_24H_EUR)}")
        print(f"[batch] merged {len(merged_syms)} candidates")

    except Exception as e:
        print("batch_collect error:", e)

def batch_loop():
    while True:
        t0 = time.time()
        batch_collect()
        time.sleep(max(5.0, BATCH_INTERVAL_SEC - (time.time()-t0)))

# ----- غرفة المراقبة -----
def ensure_metrics(sym):
    now = time.time()
    mkt = f"{sym}-EUR"
    info = metrics_cache.get(sym)
    if not info or now - info["ts"] >= 60:
        c = get_candles_1m(mkt, limit=31)
        d = changes_from_1m(c)
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
                entry_price, entry_ts, high_stored = st["entry_price"], st["entry_ts"], st["high"]
                if price > high_stored:
                    room_update_high(sym, price)
                    high_stored = price

                change_since_entry = pct(price, entry_price)

                # أعلى سعر آخر 15 دقيقة من سجل 5ث
                high15 = None
                for t, p in reversed(price_hist[sym]):
                    if now - t > 900: break
                    if high15 is None or p > high15: high15 = p
                dd_15 = pct(price, high15) if high15 else 0.0   # سالبة لو هبوط

                cond_jump  = (mc["ch5"] >= JUMP_5M_PCT and mc["spike"] >= SPIKE_WEAK)
                cond_break = (mc["high30"] > 0 and pct(price, mc["high30"]) >= BREAKOUT_30M_PCT)
                is_safe    = (change_since_entry >= 0.0)
                not_weak15 = (dd_15 >= -1.0)

                if not_weak15 and is_safe and (cond_jump or cond_break) and not in_cooldown(sym):
                    entered_at = datetime.fromtimestamp(entry_ts).strftime("%H:%M")
                    reason = "قفزة5م+سبايك" if cond_jump else "كسر30د"
                    msg = (f"🚀 {sym} | {reason} | منذ الدخول {change_since_entry:+.2f}% | "
                           f"دخل {entered_at} | spike×{mc['spike']:.1f} | 5m {mc['ch5']:+.2f}%")
                    tg(msg)
                    notify_saqr(sym)
                    mark_cooldown(sym)

            time.sleep(SCAN_INTERVAL_SEC)
        except Exception as e:
            print("monitor_room error:", e)
            time.sleep(SCAN_INTERVAL_SEC)

# =========================
# HTTP + أوامر
# =========================
@app.route("/", methods=["GET"])
def alive(): return "Room bot is alive ✅", 200

def _do_reset():
    syms = list(r.smembers(KEY_WATCH_SET))
    for b in syms:
        s = b.decode()
        r.delete(KEY_COIN_HASH(s)); r.delete(KEY_COOLDOWN(s)); r.srem(KEY_WATCH_SET, s)
    r.delete(KEY_MARKETS_CACHE); r.delete(KEY_24H_CACHE)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        txt = (data.get("message", {}).get("text") or "").strip().lower()
        if txt in ("ابدأ","start"):
            start_background(); tg("✅ تم تشغيل غرفة العمليات.")
        elif txt in ("السجل","log"):
            syms = room_members(); tg("📋 غرفة المراقبة: " + (", ".join(sorted(syms)) if syms else "فارغة"))
        elif txt in ("مسح","reset"):
            _do_reset(); tg("🧹 تم مسح الغرفة وكل الكاش. بداية جديدة.")
        return "ok", 200
    except Exception as e:
        print("webhook error:", e); return "ok", 200

# =========================
# تشغيل الخلفيات (يعمل مع gunicorn)
# =========================
def start_background():
    global _bg_started
    if _bg_started: return
    _bg_started = True
    Thread(target=batch_loop, daemon=True).start()
    Thread(target=monitor_room, daemon=True).start()
    print("Background loops started.")

# ابدأ تلقائيًا عند الاستيراد (gunicorn)
if os.getenv("DISABLE_AUTO_START", "0") != "1":
    start_background()