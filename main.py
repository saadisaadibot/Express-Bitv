# -*- coding: utf-8 -*-
import os, time, json, requests, redis
from flask import Flask, request
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from collections import deque, defaultdict
from datetime import datetime

# =========================
# إعدادات قابلة للتعديل
# =========================
BATCH_INTERVAL_SEC = int(os.getenv("BATCH_INTERVAL_SEC", 300))   # كل 15 دقيقة
ROOM_TTL_SEC       = int(os.getenv("ROOM_TTL_SEC", 3*3600))      # بقاء العملة في الغرفة
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", 5))      # مراقبة كل 5 ثواني
THREADS            = int(os.getenv("THREADS", 32))
CANDLE_TIMEOUT     = int(os.getenv("CANDLE_TIMEOUT", 10))
TICKER_TIMEOUT     = int(os.getenv("TICKER_TIMEOUT", 6))
MIN_24H_EUR        = float(os.getenv("MIN_24H_EUR", 3000))      # فلترة سيولة يومية
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC", 300))         # تبريد إشعار لكل عملة
REARM_PCT          = float(os.getenv("REARM_PCT", 1.5))          # إعادة تسليح بعد +1.5%
SPIKE_WEAK         = float(os.getenv("SPIKE_WEAK", 1.3))
JUMP_5M_PCT        = float(os.getenv("JUMP_5M_PCT", 1.5))        # قفزة 5م
BREAKOUT_30M_PCT   = float(os.getenv("BREAKOUT_30M_PCT", 0.8))   # كسر قمة 30د

# مفاتيح التشغيل
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHAT_ID       = os.getenv("CHAT_ID")
REDIS_URL     = os.getenv("REDIS_URL")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")

# Flask + Redis + Session
app  = Flask(__name__)
r    = redis.from_url(REDIS_URL) if REDIS_URL else None
sess = requests.Session()
lock = Lock()

# مفاتيح Redis
NS                = os.getenv("REDIS_NS", "room")
KEY_WATCH_SET     = f"{NS}:watch"                     # SET للأعضاء
KEY_COIN_HASH     = lambda s: f"{NS}:coin:{s}"        # HASH لكل عملة
KEY_COOLDOWN      = lambda s: f"{NS}:cool:{s}"        # تبريد ثانوي
KEY_MARKETS_CACHE = f"{NS}:markets"                   # كاش الأسواق ساعة
KEY_24H_CACHE     = f"{NS}:24h"                       # كاش سيولة 5 د

# هياكل داخلية
price_hist    = defaultdict(lambda: deque(maxlen=360))  # (ts, price) كل 5 ثوانٍ ≈ 30د
metrics_cache = {}  # sym -> {"ts","ch5","spike","close","high30"}
_bg_started   = False

# ===== مراسلة =====
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

# ===== Bitvavo helpers =====
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

# ===== حسابات من شموع 1m =====
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

# ===== إدارة الغرفة =====
# أعلى التعاريف
KEY_SEQ = f"{NS}:seq"  # عدّاد تسلسلي عام

def room_add(sym, entry_price, pts_add=0):
    hkey = KEY_COIN_HASH(sym)
    now  = int(time.time())

    if r.exists(hkey):
        # تحديث نقاط تراكمية + نقاط هذه الدفعة وتوقيتها
        try: cur = float(r.hget(hkey, "pts") or b"0")
        except Exception: cur = 0.0
        r.hset(hkey, mapping={
            "pts": f"{cur + float(pts_add):.4f}",
            "last_pts_add": str(int(pts_add)),
            "last_pts_ts": str(now),
        })
        r.expire(hkey, ROOM_TTL_SEC)
        r.sadd(KEY_WATCH_SET, sym)
        return

    # أول دخول: خذ رقم تسلسلي ثابت
    seq = r.incr(KEY_SEQ)
    p = r.pipeline()
    p.hset(hkey, mapping={
        "entry_price": f"{entry_price:.12f}",
        "entry_ts": str(now),
        "high": f"{entry_price:.12f}",
        "pts": f"{float(pts_add):.4f}",
        "seq": str(seq),
        "last_pts_add": str(int(pts_add)),
        "last_pts_ts": str(now),
    })
    p.expire(hkey, ROOM_TTL_SEC)
    p.sadd(KEY_WATCH_SET, sym)
    p.execute()

def room_get(sym):
    data = r.hgetall(KEY_COIN_HASH(sym))
    if not data: return None
    try:
        return {
            "entry_price": float(data.get(b"entry_price", b"0").decode() or "0"),
            "entry_ts": int(data.get(b"entry_ts", b"0").decode() or "0"),
            "high": float(data.get(b"high", b"0").decode() or "0"),
            "pts": float(data.get(b"pts", b"0").decode() or "0"),
            "seq": int(data.get(b"seq", b"0").decode() or "0"),
            "last_pts_add": int(data.get(b"last_pts_add", b"0").decode() or "0"),
            "last_pts_ts": int(data.get(b"last_pts_ts", b"0").decode() or "0"),
        }
    except Exception:
        return None

def room_update_high(sym, v): r.hset(KEY_COIN_HASH(sym), "high", f"{v:.12f}")
def in_cooldown(sym): return bool(r.get(KEY_COOLDOWN(sym)))
def mark_cooldown(sym): r.setex(KEY_COOLDOWN(sym), COOLDOWN_SEC, 1)

def get_last_alert(sym):
    d = r.hgetall(KEY_COIN_HASH(sym))
    if not d: return 0, 0.0, ""
    ts  = int(d.get(b"last_alert_ts", b"0").decode() or "0")
    prc = float(d.get(b"last_alert_price", b"0").decode() or "0")
    rsn = d.get(b"last_alert_reason", b"").decode() if b"last_alert_reason" in d else ""
    return ts, prc, rsn

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

# ===== لقطة حيّة قبل الإرسال =====
def fresh_snapshot(sym):
    mkt = f"{sym}-EUR"
    c = get_candles_1m(mkt, limit=31)
    d = changes_from_1m(c) if c else None
    px = get_ticker_price(mkt)
    if d:
        if px <= 0: px = d["close"]
        return {"price": px, "ch5": d["ch_5m"], "spike": d["spike"], "high30": d["high30"], "close": d["close"]}
    return {"price": px or 0.0, "ch5": 0.0, "spike": 1.0, "high30": 0.0, "close": 0.0}

# =========================
# دفعة التجميع (Top5 من كل فريم)
# =========================
def batch_collect():
    try:
        # أسواق
        markets_b = r.get(KEY_MARKETS_CACHE)
        markets = json.loads(markets_b.decode() if isinstance(markets_b, (bytes, bytearray)) else markets_b) \
                  if markets_b else get_markets_eur()
        if not markets_b: r.setex(KEY_MARKETS_CACHE, 3600, json.dumps(markets))

        # سيولة 24h
        vol24_b = r.get(KEY_24H_CACHE)
        vol24 = json.loads(vol24_b.decode() if isinstance(vol24_b, (bytes, bytearray)) else vol24_b) \
                if vol24_b else get_24h_stats_eur()
        if not vol24_b: r.setex(KEY_24H_CACHE, 300, json.dumps(vol24))
        vol_filter_active = bool(vol24)

        # جلب الشموع
        def fetch_one(market):
            c = get_candles_1m(market, limit=60)
            d = changes_from_1m(c)
            if not d: return None
            if vol_filter_active and vol24.get(market, 0.0) < MIN_24H_EUR: return None
            return market, d

        rows = []
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            for res in ex.map(fetch_one, markets):
                if res: rows.append(res)
        if not rows:
            print(f"batch_collect: no rows (markets={len(markets)}, vol24={'ok' if vol_filter_active else 'empty'})")
            return

        # تكوين Top5 لكل فريم
        ranks = {"5m":[], "15m":[], "30m":[], "1h":[]}
        for market, d in rows:
            ranks["5m"].append((market, d["ch_5m"], d))
            ranks["15m"].append((market, d["ch_15m"], d))
            ranks["30m"].append((market, d["ch_30m"], d))
            ranks["1h"].append((market, d["ch_1h"], d))
        for k in ranks:
            ranks[k].sort(key=lambda x: x[1], reverse=True)
            ranks[k] = ranks[k][:5]

        # نقاط الظهور 1..4
        appear_pts = defaultdict(int)
        pick_map   = {}
        for arr in ranks.values():
            for market, _, d in arr:
                sym = market.replace("-EUR","")
                appear_pts[sym] += 1
                pick_map.setdefault(sym, (market, d))

        merged_syms = list(appear_pts.keys())

        # إدخال/تحديث الغرفة + النقاط التراكمية
        for sym in merged_syms:
            mkt, d = pick_map[sym]
            entry_price = get_ticker_price(mkt) or d["close"]
            if entry_price > 0:
                room_add(sym, entry_price, appear_pts[sym])

        tg(f"✅ تحديث المرشحين: {len(merged_syms)} عملة | Top5 لكل 5m/15m/30m/1h")
        print(f"[batch] merged {len(merged_syms)} candidates")

    except Exception as e:
        print("batch_collect error:", e)

def batch_loop():
    while True:
        t0 = time.time()
        batch_collect()
        time.sleep(max(5.0, BATCH_INTERVAL_SEC - (time.time()-t0)))

# =========================
# المراقبة الذكية (كل 5 ثواني)
# =========================
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
                entry_price, entry_ts, high_stored, pts = st["entry_price"], st["entry_ts"], st["high"], int(round(st["pts"]))

                if price > high_stored:
                    room_update_high(sym, price)
                    high_stored = price

                # أعلى سعر آخر 15د من سجل 5ث
                high15 = None
                for t, p in reversed(price_hist[sym]):
                    if now - t > 900: break
                    if high15 is None or p > high15: high15 = p
                dd_15 = pct(price, high15) if high15 else 0.0

                # شروط الدخول
                cond_jump  = (mc["ch5"] >= JUMP_5M_PCT and mc["spike"] >= SPIKE_WEAK)
                cond_break = (mc["high30"] > 0 and pct(price, mc["high30"]) >= BREAKOUT_30M_PCT)
                not_weak15 = (dd_15 >= -1.0)
                change_since_entry = pct(price, entry_price)

                reason = "قفزة5م+سبايك" if cond_jump else "كسر30د"

                if not_weak15 and change_since_entry >= 0.0 and (cond_jump or cond_break):
                    # لقطة حيّة قبل الإرسال
                    snap = fresh_snapshot(sym)
                    price = snap["price"]
                    mc["ch5"], mc["spike"], mc["high30"] = snap["ch5"], snap["spike"], snap["high30"]
                    change_since_entry = pct(price, entry_price)

                    la_ts, la_price, la_reason = get_last_alert(sym)
                    ok_time   = (time.time() - la_ts) >= COOLDOWN_SEC
                    ok_move   = (la_price == 0) or (price >= la_price * (1 + REARM_PCT/100.0))
                    ok_reason = (la_reason != reason)

                    if ok_time and (ok_move or ok_reason) and not in_cooldown(sym):
                        entered_at = datetime.fromtimestamp(entry_ts).strftime("%H:%M")
                        msg = (f"🚀 {sym} / {pts} نقاط | {reason} | منذ الدخول {change_since_entry:+.2f}% | "
                               f"دخل {entered_at} | spikex{mc['spike']:.1f} | 5m {mc['ch5']:+.2f}%")
                        tg(msg); notify_saqr(sym)
                        mark_cooldown(sym)
                        set_last_alert(sym, int(time.time()), price, reason)

            time.sleep(SCAN_INTERVAL_SEC)
        except Exception as e:
            print("monitor_room error:", e)
            time.sleep(SCAN_INTERVAL_SEC)

# =========================
# HTTP + أوامر
# =========================
@app.route("/", methods=["GET"])
def alive():
    return "Room bot is alive ✅", 200

def _do_reset(full=False):
    syms = list(r.smembers(KEY_WATCH_SET))
    for b in syms:
        s = b.decode()
        r.delete(KEY_COIN_HASH(s)); r.delete(KEY_COOLDOWN(s)); r.srem(KEY_WATCH_SET, s)
    if full:
        r.delete(KEY_MARKETS_CACHE); r.delete(KEY_24H_CACHE)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        txt = (data.get("message", {}).get("text") or "").strip().lower()
        if txt in ("ابدأ","start"):
            start_background(); tg("✅ تم تشغيل غرفة العمليات.")
        elif txt in ("السجل","log"):
        syms = room_members()
        rows = []
        now = int(time.time())
        for s in syms:
            d = r.hgetall(KEY_COIN_HASH(s))
            pts = float(d.get(b"pts", b"0").decode() or "0")
            seq = int(d.get(b"seq", b"0").decode() or "0")
            last_add = int(d.get(b"last_pts_add", b"0").decode() or "0")
            last_ts  = int(d.get(b"last_pts_ts", b"0").decode() or "0")
            recent = (now - last_ts) <= (BATCH_INTERVAL_SEC + 120)  # اعتبرها “دفعة حالية”
            rows.append((s, pts, seq, last_add, recent))

    # رتّب بالنقاط نزولاً
        rows.sort(key=lambda x: x[1], reverse=True)

        lines = [f"📊 مراقبة {len(rows)} عملة:"]
        for i,(s,pts,seq,last_add,recent) in enumerate(rows, start=1):
            flag = " 🆕" if recent and last_add > 0 else ""
            delta = f" +{last_add}" if last_add > 0 else ""
            lines.append(f"{i}. {s} / {int(round(pts))} نقاط  [#{seq}{delta}]{flag}")
        tg("\n".join(lines))
        elif txt in ("مسح","reset"):
            _do_reset(full=True); tg("🧹 تم مسح الغرفة والكاش.")
        return "ok", 200
    except Exception as e:
        print("webhook error:", e); return "ok", 200

# =========================
# تشغيل الخلفيات
# =========================
def start_background():
    global _bg_started
    if _bg_started: return
    _bg_started = True
    Thread(target=batch_loop, daemon=True).start()
    Thread(target=monitor_room, daemon=True).start()
    print("Background loops started.")

# ابدأ تلقائيًا
if os.getenv("DISABLE_AUTO_START", "0") != "1":
    start_background()

# تشغيل Flask محليًا (Railway يمرّر PORT)
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)