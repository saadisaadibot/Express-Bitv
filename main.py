# -*- coding: utf-8 -*-
import os, time, json, requests, redis
from flask import Flask, request
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from collections import deque, defaultdict
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
BATCH_INTERVAL_SEC = int(os.getenv("BATCH_INTERVAL_SEC", 900))   # كل 15 دقيقة نجمع المرشحين
ROOM_TTL_SEC       = int(os.getenv("ROOM_TTL_SEC", 3*3600))      # المرشح يبقى 3 ساعات
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", 5))      # مراقبة الغرفة كل 5 ثوانٍ
THREADS            = int(os.getenv("THREADS", 32))               # لدفعات التجميع

TOP_PER_TF         = int(os.getenv("TOP_PER_TF", 5))             # Top من كل فريم (5m/15m/30m/1h)
MIN_24H_EUR        = float(os.getenv("MIN_24H_EUR", 1000))      # حد سيولة يومية
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC", 300))         # تبريد 5 دقائق لكل عملة
STEP_WINDOW_SEC    = int(os.getenv("STEP_WINDOW_SEC", 180))      # نافذة نمط 1%+1%
STEP_PCT           = float(os.getenv("STEP_PCT", 1.0))           # كل خطوة 1%
SPIKE_WEAK         = float(os.getenv("SPIKE_WEAK", 1.3))         # spike خفيف
JUMP_5M_PCT        = float(os.getenv("JUMP_5M_PCT", 1.5))        # قفزة 5 دقائق
BREAKOUT_30M_PCT   = float(os.getenv("BREAKOUT_30M_PCT", 0.8))   # كسر قمة 30 دقيقة

# =========================
# 🔐 مفاتيح التشغيل
# =========================
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHAT_ID       = os.getenv("CHAT_ID")
REDIS_URL     = os.getenv("REDIS_URL")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")

app  = Flask(__name__)
r    = redis.from_url(REDIS_URL) if REDIS_URL else None
sess = requests.Session()
lock = Lock()

# =========================
# 🗃️ مفاتيح Redis
# =========================
NS                = os.getenv("REDIS_NS", "room")
KEY_WATCH_SET     = f"{NS}:watch"                       # SET للأعضاء
KEY_COIN_HASH     = lambda s: f"{NS}:coin:{s}"          # HASH: entry_price, entry_ts, high, points
KEY_COOLDOWN      = lambda s: f"{NS}:cool:{s}"          # تبريد الإشعارات
KEY_MARKETS_CACHE = f"{NS}:markets"                     # كاش الأسواق EUR
KEY_24H_CACHE     = f"{NS}:24h"                         # كاش سيولة 24h

# =========================
# 🧠 هياكل داخلية خفيفة
# =========================
price_hist    = defaultdict(lambda: deque(maxlen=360))  # (ts, price) كل 5 ثوانٍ ≈ 30 دقيقة
metrics_cache = {}   # sym -> {"ts":..., "ch5":..., "spike":..., "close":..., "high30":...}
step_state    = {}   # لنمط 1%+1%
last_alert    = {}   # تبريد الإشعارات
supported     = set()
_bg_started   = False

# =========================
# 📮 مراسلة
# =========================
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

# =========================
# 🔧 Bitvavo helpers
# =========================
def get_markets_eur():
    global supported
    try:
        res = sess.get("https://api.bitvavo.com/v2/markets", timeout=10).json()
        supported = {m["market"].replace("-EUR","") for m in res if m.get("market","").endswith("-EUR")}
        return [m["market"] for m in res if m.get("market","").endswith("-EUR")]
    except Exception as e:
        print("markets error:", e); return []

def get_candles_1m(market: str, limit: int = 60):
    try:
        return sess.get(
            f"https://api.bitvavo.com/v2/{market}/candles?interval=1m&limit={limit}",
            timeout=10
        ).json()
    except Exception as e:
        print("candles error:", market, e); return []

def get_ticker_price(market: str):
    try:
        data = sess.get(f"https://api.bitvavo.com/v2/ticker/price?market={market}",
                        timeout=6).json()
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

# =========================
# 🧮 حسابات من شموع 1m
# =========================
def pct(a, b):  # نسبة مئوية بين سعرين
    return ((a-b)/b*100.0) if b > 0 else 0.0

def changes_from_1m(c):
    if not isinstance(c, list) or len(c) < 6:
        return None
    closes = [float(x[4]) for x in c]
    vols   = [float(x[5]) for x in c]
    n = len(c)

    def safe(idx):
        return pct(closes[-1], closes[-idx]) if n >= idx and closes[-idx] > 0 else 0.0

    close  = closes[-1]
    ch_1m  = safe(2)
    ch_5m  = safe(6)
    ch_15m = safe(16)
    ch_30m = safe(31)
    ch_1h  = safe(60)

    k = min(15, max(1, n-1))
    base = sum(vols[-(k+1):-1]) / k if n >= 3 else 0.0
    spike = (vols[-1] / base) if base > 0 else 1.0
    high30 = max(closes[-min(31, n):]) if n else close

    return {"close": close, "ch_1m": ch_1m, "ch_5m": ch_5m, "ch_15m": ch_15m,
            "ch_30m": ch_30m, "ch_1h": ch_1h, "spike": spike, "high30": high30}

# =========================
# 🧱 إدارة الغرفة (Redis)
# =========================
def room_add(sym, entry_price, points=1):
    hkey = KEY_COIN_HASH(sym)
    now  = int(time.time())
    p = r.pipeline()
    p.hset(hkey, mapping={
        "entry_price": f"{entry_price:.12f}",
        "entry_ts": str(now),
        "high": f"{entry_price:.12f}",
        "points": str(points),
    })
    p.expire(hkey, ROOM_TTL_SEC)
    p.sadd(KEY_WATCH_SET, sym)
    p.execute()

def room_get(sym):
    data = r.hgetall(KEY_COIN_HASH(sym))
    if not data: return None
    try:
        return {
            "entry_price": float(data.get(b"entry_price", b"0").decode()),
            "entry_ts": int(data.get(b"entry_ts", b"0").decode()),
            "high": float(data.get(b"high", b"0").decode()),
            "points": int(data.get(b"points", b"1").decode())
        }
    except:
        return None

def room_update_high(sym, new_high):
    r.hset(KEY_COIN_HASH(sym), "high", f"{new_high:.12f}")

def in_cooldown(sym):  return bool(r.get(KEY_COOLDOWN(sym)))
def mark_cooldown(sym): r.setex(KEY_COOLDOWN(sym), COOLDOWN_SEC, 1)

def room_members():
    syms = list(r.smembers(KEY_WATCH_SET))
    out = []
    for b in syms:
        s = b.decode()
        if r.exists(KEY_COIN_HASH(s)):
            out.append(s)
        else:
            r.srem(KEY_WATCH_SET, s)  # تنظيف العضو المنتهي
    return out

# =========================
# 🔁 مرحلة التجميع (كل 15 دقيقة)
# =========================
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
            r.setex(KEY_24H_CACHE, 300, json.dumps(vol24))  # كل 5 دقائق

        # جلب شموع 1m/60 لكل زوج (دفعات)
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

        # ترتيب Top5 لكل فريم
        ranks = {"5m":[], "15m":[], "30m":[], "1h":[]}
        for market, d in rows:
            ranks["5m"].append((market, d["ch_5m"], d))
            ranks["15m"].append((market, d["ch_15m"], d))
            ranks["30m"].append((market, d["ch_30m"], d))
            ranks["1h"].append((market, d["ch_1h"], d))
        for k in ranks:
            ranks[k].sort(key=lambda x: x[1], reverse=True)
            ranks[k] = ranks[k][:TOP_PER_TF]

        # حساب "النقاط" = عدد الفريمات التي ظهرت فيها العملة (1–4)
        points = defaultdict(int)  # sym -> pts
        pick_map = {}              # sym -> (market, d) لأخذ سعر الدخول
        for tf, arr in ranks.items():
            for market, _, d in arr:
                sym = market.replace("-EUR", "")
                points[sym] += 1
                pick_map.setdefault(sym, (market, d))

        merged_syms = list(points.keys())

        # إدخال المرشحين للغرفة مع النقاط
        for sym in merged_syms:
            mkt, d = pick_map[sym]
            entry_price = get_ticker_price(mkt) or d["close"]
            if entry_price > 0:
                room_add(sym, entry_price, points[sym])

        tg(f"✅ تحديث المرشحين: {len(merged_syms)} عملة | من Top5 لكل 5m/15m/30m/1h")
        print(f"[batch] merged {len(merged_syms)} candidates")

    except Exception as e:
        print("batch_collect error:", e)

def batch_loop():
    while True:
        t0 = time.time()
        batch_collect()
        spent = time.time() - t0
        time.sleep(max(5.0, BATCH_INTERVAL_SEC - spent))

# =========================
# 👁️‍🗨️ المراقبة الذكية (كل 5 ثوانٍ)
# =========================
def ensure_metrics(sym):
    now = time.time()
    mkt = f"{sym}-EUR"
    info = metrics_cache.get(sym)
    # كل ~60ث نحدّث 1m/31 (تكفي لـ 5m + spike + high30)
    if not info or now - info["ts"] >= 60:
        c = get_candles_1m(mkt, limit=31)
        d = changes_from_1m(c)
        if d:
            metrics_cache[sym] = {
                "ts": now,
                "ch5": d["ch_5m"],
                "spike": d["spike"],
                "close": d["close"],
                "high30": d["high30"]
            }
        else:
            price = get_ticker_price(mkt)
            metrics_cache[sym] = {
                "ts": now, "ch5": 0.0, "spike": 1.0, "close": price, "high30": price
            }

def in_step_pattern(sym, price_now, ts):
    """نمط 1% + 1% خلال STEP_WINDOW_SEC"""
    st = step_state.get(sym)
    if st is None:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False
    if ts - st["t0"] > STEP_WINDOW_SEC:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False
    base = st["base"]
    if base <= 0:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False
    step_pct_now = (price_now - base) / base * 100
    if step_pct_now < -0.5:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False
    if not st["p1"]:
        if step_pct_now >= STEP_PCT:
            st["p1"] = (price_now, ts)
        return False
    else:
        p1_price, _ = st["p1"]
        if (price_now - p1_price) / p1_price * 100 >= STEP_PCT:
            step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
            return True
    return False

def should_alert(sym): return (time.time() - last_alert.get(sym, 0)) >= COOLDOWN_SEC
def mark_alerted(sym):  last_alert[sym] = time.time()

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
                entry_price, entry_ts, high_stored, pts = st["entry_price"], st["entry_ts"], st["high"], st["points"]

                if price > high_stored:
                    room_update_high(sym, price)
                    high_stored = price

                change_since_entry = pct(price, entry_price)

                # أعلى سعر آخر 15 دقيقة من سجل 5 ثوانٍ
                high15 = None
                for t, p in list(price_hist[sym])[::-1]:
                    if now - t > 900: break
                    if high15 is None or p > high15: high15 = p
                dd_15 = pct(price, high15) if high15 else 0.0  # سالبة عند الهبوط

                # شروط الإطلاق
                cond_jump  = (mc["ch5"] >= JUMP_5M_PCT and mc["spike"] >= SPIKE_WEAK)
                cond_break = (mc["high30"] > 0 and pct(price, mc["high30"]) >= BREAKOUT_30M_PCT)
                is_safe    = (change_since_entry >= 0.0)
                not_weak15 = (dd_15 >= -1.0)

                should_buy = (not_weak15 and is_safe and (cond_jump or cond_break))

                if should_buy and not in_cooldown(sym):
                    entered_at = datetime.fromtimestamp(entry_ts).strftime("%H:%M")
                    reason = "قفزة5م+سبايك" if cond_jump else "كسر30د"
                    # سطر مختصر + يظهر النقاط
                    msg = (f"🚀 {sym} / {pts} نقاط | {reason} | منذ الدخول {change_since_entry:+.2f}% | "
                           f"دخل {entered_at} | spike×{mc['spike']:.1f} | 5m {mc['ch5']:+.2f}%")
                    tg(msg)
                    notify_saqr(sym)
                    mark_cooldown(sym)

            time.sleep(SCAN_INTERVAL_SEC)
        except Exception as e:
            print("monitor_room error:", e)
            time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 🌐 HTTP + أوامر
# =========================
@app.route("/", methods=["GET"])
def alive():
    return "Room bot is alive ✅", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        txt = (data.get("message", {}).get("text") or "").strip().lower()

        if txt in ("ابدأ", "start"):
            start_background()
            tg("✅ تم تشغيل غرفة العمليات.")

        elif txt in ("السجل", "log"):
            syms = room_members()
            rows = []
            for s in syms:
                st = room_get(s)
                if st: rows.append((s, st.get("points", 1)))
            rows.sort(key=lambda x: x[1], reverse=True)
            lines = [f"📊 مراقبة {len(rows)} عملة:"]
            for i, (s, pts) in enumerate(rows, start=1):
                lines.append(f"{i}. {s} / {pts} نقاط")
            tg("\n".join(lines))

        elif txt in ("مسح", "reset"):
            # مسح المرشحين
            syms = list(r.smembers(KEY_WATCH_SET))
            for b in syms:
                s = b.decode()
                r.delete(KEY_COIN_HASH(s))
                r.delete(KEY_COOLDOWN(s))
                r.srem(KEY_WATCH_SET, s)
            # مسح أي كاش
            r.delete(KEY_MARKETS_CACHE)
            r.delete(KEY_24H_CACHE)
            tg("🧹 تم مسح الغرفة وكل الكاش. بداية جديدة.")

        return "ok", 200
    except Exception as e:
        print("webhook error:", e)
        return "ok", 200

# =========================
# 🚀 التشغيل الخلفي + الإقلاع
# =========================
def start_background():
    global _bg_started
    if _bg_started: return
    _bg_started = True
    Thread(target=batch_loop, daemon=True).start()
    Thread(target=monitor_room, daemon=True).start()
    print("Background loops started.")

if __name__ == "__main__":
    start_background()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)