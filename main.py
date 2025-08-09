# -*- coding: utf-8 -*-
import os, time, json, math, requests, redis
from flask import Flask, request
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from collections import deque, defaultdict
from datetime import datetime

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
BATCH_INTERVAL_SEC    = int(os.getenv("BATCH_INTERVAL_SEC", 900))   # كل 15 دقيقة نجمع المرشحين
ROOM_TTL_SEC          = int(os.getenv("ROOM_TTL_SEC", 3*3600))      # المرشح يبقى 3 ساعات
TOP_MERGED            = int(os.getenv("TOP_MERGED", 20))            # عدد المرشحين لدخول الغرفة
SCAN_INTERVAL_SEC     = int(os.getenv("SCAN_INTERVAL_SEC", 5))      # مراقبة الغرفة كل 5 ثواني
CANDLE_TIMEOUT        = int(os.getenv("CANDLE_TIMEOUT", 10))
TICKER_TIMEOUT        = int(os.getenv("TICKER_TIMEOUT", 6))
THREADS               = int(os.getenv("THREADS", 32))               # لدفعات التجميع
MIN_24H_EUR           = float(os.getenv("MIN_24H_EUR", 10000))      # فلتر سيولة يومية
COOLDOWN_SEC          = int(os.getenv("COOLDOWN_SEC", 300))         # تبريد 5 دقائق لكل عملة
SPIKE_WEAK            = float(os.getenv("SPIKE_WEAK", 1.3))         # spike خفيف
SPIKE_STRONG          = float(os.getenv("SPIKE_STRONG", 1.6))       # spike قوي
JUMP_5M_PCT           = float(os.getenv("JUMP_5M_PCT", 1.5))        # قفزة 5 دقائق
BREAKOUT_30M_PCT      = float(os.getenv("BREAKOUT_30M_PCT", 0.8))   # كسر قمة 30 دقيقة
LEADER_MIN_PCT        = float(os.getenv("LEADER_MIN_PCT", 5.0))     # حد القائد منذ الدخول
WEIGHTS_T             = (0.45, 0.35, 0.20)  # 1h, 30m, 15m
WEIGHTS_A             = (0.40, 0.45, 0.15)  # 15m, 5m, 1m

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
NS                = os.getenv("REDIS_NS", "room")       # بادئة كل المفاتيح
KEY_WATCH_SET     = f"{NS}:watch"                       # SET للأعضاء (أسماء فقط)
KEY_COIN_HASH     = lambda s: f"{NS}:coin:{s}"          # HASH: entry_price, entry_ts, high, last_alert_ts
KEY_COOLDOWN      = lambda s: f"{NS}:cool:{s}"          # تبريد الإشعارات
KEY_MARKETS_CACHE = f"{NS}:markets"                     # كاش الأسواق EUR
KEY_24H_CACHE     = f"{NS}:24h"                         # كاش سيولة 24h

# =========================
# 🧠 هياكل داخلية خفيفة
# =========================
price_hist  = defaultdict(lambda: deque(maxlen=360))    # (ts, price) كل 5 ثواني ≈ 30 دقيقة
metrics_cache = {}  # sym -> {"ts":..., "ch5":..., "spike":..., "close":..., "high30":...}
supported   = set()

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
    try:
        res = sess.get("https://api.bitvavo.com/v2/markets", timeout=CANDLE_TIMEOUT).json()
        markets = [m["market"] for m in res if m.get("market","").endswith("-EUR")]
        return markets
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

# =========================
# 🧮 حساب التغيرات من شموع 1m
# =========================
def pct(a, b):
    return ((a-b)/b*100.0) if b > 0 else 0.0

def changes_from_1m(c):
    # c: [[t,o,h,l,close,vol], ...] طولها >= 60
    if not isinstance(c, list) or len(c) < 60:
        return None
    closes = [float(x[4]) for x in c]
    vols   = [float(x[5]) for x in c]
    close  = closes[-1]
    ch_1m  = pct(closes[-1], closes[-2])
    ch_5m  = pct(closes[-1], closes[-6])
    ch_15m = pct(closes[-1], closes[-16])
    ch_30m = pct(closes[-1], closes[-31])
    ch_1h  = pct(closes[-1], closes[-60])
    # spike: آخر دقيقة/متوسط 15 دقيقة قبلها
    avg15  = sum(vols[-16:-1]) / 15.0 if sum(vols[-16:-1]) > 0 else 0.0
    spike  = (vols[-1] / avg15) if avg15 > 0 else 1.0
    high30 = max(closes[-31:])  # أعلى إغلاق آخر 30 دقيقة
    return {
        "close": close,
        "ch_1m": ch_1m,
        "ch_5m": ch_5m,
        "ch_15m": ch_15m,
        "ch_30m": ch_30m,
        "ch_1h": ch_1h,
        "spike": spike,
        "high30": high30
    }

# =========================
# 🧪 ترجيح متعدد الفريمات
# =========================
def rank_score_T(ch_1h, ch_30m, ch_15m):
    # ترتيب كنقاط مباشرة (بدون RankPct لتبسيط وسرعة)
    w1, w2, w3 = WEIGHTS_T
    return w1*ch_1h + w2*ch_30m + w3*ch_15m

def accel_score_A(ch_15m, ch_5m, ch_1m, spike):
    w1, w2, w3 = WEIGHTS_A
    bonus = 0.10 if spike >= SPIKE_STRONG else (0.05 if spike >= SPIKE_WEAK else 0.0)
    return w1*ch_15m + w2*ch_5m + w3*ch_1m + 100*bonus  # نعظّم البونص ليظهر بوضوح

def merged_score(T, A):
    return 0.6*T + 0.4*A

# =========================
# 🧱 إدارة الغرفة (Redis)
# =========================
def room_add(sym, entry_price):
    hkey = KEY_COIN_HASH(sym)
    now  = int(time.time())
    pipe = r.pipeline()
    pipe.hset(hkey, mapping={
        "entry_price": f"{entry_price:.12f}",
        "entry_ts": str(now),
        "high": f"{entry_price:.12f}",
    })
    pipe.expire(hkey, ROOM_TTL_SEC)
    pipe.sadd(KEY_WATCH_SET, sym)
    pipe.execute()

def room_get(sym):
    data = r.hgetall(KEY_COIN_HASH(sym))
    if not data: return None
    try:
        return {
            "entry_price": float(data.get(b"entry_price", b"0").decode()),
            "entry_ts": int(data.get(b"entry_ts", b"0").decode()),
            "high": float(data.get(b"high", b"0").decode())
        }
    except:
        return None

def room_update_high(sym, new_high):
    r.hset(KEY_COIN_HASH(sym), "high", f"{new_high:.12f}")

def in_cooldown(sym):
    return bool(r.get(KEY_COOLDOWN(sym)))

def mark_cooldown(sym):
    r.setex(KEY_COOLDOWN(sym), COOLDOWN_SEC, 1)

def room_members():
    # نرجع فقط اللي مفاتيحهم موجودة (TTL ما انتهى)
    syms = list(r.smembers(KEY_WATCH_SET))
    out = []
    for b in syms:
        s = b.decode()
        if r.exists(KEY_COIN_HASH(s)):
            out.append(s)
        else:
            r.srem(KEY_WATCH_SET, s)  # تنظيف
    return out

# =========================
# 🔁 مرحلة التجميع (كل 15 دقيقة)
# =========================
def batch_collect():
    try:
        markets = r.get(KEY_MARKETS_CACHE)
        if markets:
            markets = json.loads(markets)
        else:
            markets = get_markets_eur()
            r.setex(KEY_MARKETS_CACHE, 3600, json.dumps(markets))

        # سيولة يومية
        vol24 = r.get(KEY_24H_CACHE)
        if vol24:
            vol24 = json.loads(vol24)
        else:
            vol24 = get_24h_stats_eur()
            r.setex(KEY_24H_CACHE, 300, json.dumps(vol24))  # كل 5 دقائق

        # نجلب شموع 1m/60 لكل زوج (دفعات)
        def fetch_one(market):
            c = get_candles_1m(market, limit=60)
            d = changes_from_1m(c)
            if not d: return None
            # فلتر سيولة
            if vol24.get(market, 0.0) < MIN_24H_EUR:
                return None
            return market, d

        rows = []
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            for res in ex.map(fetch_one, markets):
                if res: rows.append(res)

        if not rows:
            print("batch_collect: no rows"); return

        # حساب درجات
        scored = []
        for market, d in rows:
            T = rank_score_T(d["ch_1h"], d["ch_30m"], d["ch_15m"])
            A = accel_score_A(d["ch_15m"], d["ch_5m"], d["ch_1m"], d["spike"])
            S = merged_score(T, A)
            scored.append((market, S, d))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:TOP_MERGED]

        # إدخال للغرفة
        for market, S, d in top:
            sym = market.replace("-EUR", "")
            entry_price = get_ticker_price(market) or d["close"]
            room_add(sym, entry_price)

        tg(f"✅ تحديث غرفة المرشحين: {len(top)} عملة | كل 15د | سيولة≥€{int(MIN_24H_EUR)}")
        print(f"[batch] updated {len(top)} candidates")

    except Exception as e:
        print("batch_collect error:", e)

def batch_loop():
    while True:
        t0 = time.time()
        batch_collect()
        spent = time.time() - t0
        wait  = max(5.0, BATCH_INTERVAL_SEC - spent)
        time.sleep(wait)

# =========================
# 👁️‍🗨️ مرحلة المراقبة (كل 5 ثواني)
# =========================
def ensure_metrics(sym):
    now = time.time()
    mkt = f"{sym}-EUR"
    info = metrics_cache.get(sym)
    # حدّث كل ~60 ثانية بالشموع 1m/31 (للـ 5m + spike + high30)
    if not info or now - info["ts"] >= 60:
        c = get_candles_1m(mkt, limit=31)
        d = changes_from_1m(c + [c[-1]]) if isinstance(c, list) and len(c) >= 31 else None  # hack بسيط لضمان المفاتيح
        if d:
            metrics_cache[sym] = {
                "ts": now,
                "ch5": d["ch_5m"],
                "spike": d["spike"],
                "close": d["close"],
                "high30": d["high30"]
            }
        else:
            # fallback: سعر الآن فقط
            price = get_ticker_price(mkt)
            metrics_cache[sym] = {
                "ts": now, "ch5": 0.0, "spike": 1.0, "close": price, "high30": price
            }

def monitor_room():
    while True:
        try:
            syms = room_members()
            now  = time.time()
            for sym in syms:
                mkt = f"{sym}-EUR"
                # 1) تحديث المقاييس الثقيلة (كل ~60ث)
                ensure_metrics(sym)
                mc = metrics_cache.get(sym, {"ch5":0.0,"spike":1.0,"close":0.0,"high30":0.0})

                # 2) سعر الآن (خفيف) + تحديث سجل 5 ثواني
                price = get_ticker_price(mkt) or mc["close"]
                price_hist[sym].append((now, price))

                # 3) بيانات الغرفة
                st = room_get(sym)
                if not st:
                    continue
                entry_price = st["entry_price"]
                entry_ts    = st["entry_ts"]
                high_stored = st["high"]

                # حدّث أعلى سعر منذ الدخول
                if price > high_stored:
                    room_update_high(sym, price)
                    high_stored = price

                change_since_entry = pct(price, entry_price)

                # هبوط آخر 15 دقيقة (من سجل 5 ثواني)
                # نأخذ أعلى سعر خلال آخر 900ث ثم نقارن بالآن
                high15 = None
                for t, p in list(price_hist[sym])[::-1]:
                    if now - t > 900: break
                    if high15 is None or p > high15:
                        high15 = p
                dd_15 = pct(price, high15) if high15 else 0.0  # قيمة سالبة عند الهبوط

                # شروط الدخول:
                cond_jump = (mc["ch5"] >= JUMP_5M_PCT and mc["spike"] >= SPIKE_WEAK)
                cond_break = (mc["high30"] > 0 and pct(price, mc["high30"]) >= BREAKOUT_30M_PCT)

                # تصنيف
                is_leader = (change_since_entry >= LEADER_MIN_PCT)
                is_safe   = (change_since_entry >= 0.0)  # فوق دخول الغرفة

                # حماية: لا نرسل إذا هبوط آخر 15د ≤ -1%
                not_weak15 = (dd_15 >= -1.0)

                should_buy = (not_weak15 and is_safe and (cond_jump or cond_break))

                if should_buy and not in_cooldown(sym):
                    # تنسيق سطر واحد مختصر
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
# 🌐 Healthcheck + Webhook بسيط
# =========================
@app.route("/", methods=["GET"])
def alive():
    return "Room bot is alive ✅", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        txt = (data.get("message", {}).get("text") or "").strip().lower()
        if txt in ("ابدأ","start"):
            Thread(target=batch_loop, daemon=True).start()
            Thread(target=monitor_room, daemon=True).start()
            tg("✅ تم تشغيل غرفة العمليات.")
        elif txt in ("السجل","log"):
            syms = room_members()
            tg("📋 غرفة المراقبة: " + (", ".join(sorted(syms)) if syms else "فارغة"))
        elif txt in ("مسح","reset"):
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
# 🚀 الإقلاع
# =========================
def boot():
    # بدء حلقات التشغيل تلقائيًا
    Thread(target=batch_loop, daemon=True).start()
    Thread(target=monitor_room, daemon=True).start()

if __name__ == "__main__":
    boot()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)