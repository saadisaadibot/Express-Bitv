# -*- coding: utf-8 -*-
import os, time, requests, redis
from collections import deque, defaultdict
from threading import Thread
from flask import Flask, request

# =========================
# 📌 إعدادات الإشعار (تعديل سريع)
# =========================
TRUST_TOP_N              = int(os.getenv("TRUST_TOP_N", 10))   # قائمة الثقة
TRUST_WARMUP_SCANS       = int(os.getenv("TRUST_WARMUP_SCANS", 3))
TRUST_CH5_DELTA          = float(os.getenv("TRUST_CH5_DELTA", -0.3))
TRUST_SPIKE_DELTA        = float(os.getenv("TRUST_SPIKE_DELTA", -0.2))
TRUST_MOVE_DELTA         = float(os.getenv("TRUST_MOVE_DELTA", -0.1))
GLOBAL_WARMUP_CYCLES     = int(os.getenv("GLOBAL_WARMUP_CYCLES", 2))

MIN_CH5_FOR_ALERT        = float(os.getenv("MIN_CH5_FOR_ALERT", 0.7))
MIN_SPIKE_FOR_ALERT      = float(os.getenv("MIN_SPIKE_FOR_ALERT", 1.1))
MIN_MOVE_FROM_ENTRY      = float(os.getenv("MIN_MOVE_FROM_ENTRY", 0.25))

COOLDOWN_SEC             = int(os.getenv("COOLDOWN_SEC", 300))     # تبريد نفس العملة
REARM_PCT                = float(os.getenv("REARM_PCT", 1.5))      # لازم يتجاوز آخر سعر إشعار

# مانع الوهم / خصم
DROP_DEMERIT_PCT         = float(os.getenv("DROP_DEMERIT_PCT", -2.0))   # هبوط من القمّة يخصم نقاط
DROP_DEMERIT_POINTS      = float(os.getenv("DROP_DEMERIT_POINTS", 3.0))
DROP_DEMERIT_COOLDOWN    = int(os.getenv("DROP_DEMERIT_COOLDOWN", 30))
REMOVE_IF_LOST_PCT       = float(os.getenv("REMOVE_IF_LOST_PCT", 50.0)) # حذف لو خسر ≥50% نقاطه

# ملاحقة الانفجار الحقيقي (Fast-Lane)
FAST_CH5                 = float(os.getenv("FAST_CH5", 1.0))
FAST_SPIKE               = float(os.getenv("FAST_SPIKE", 1.5))
MICROTREND_MIN_BARS      = int(os.getenv("MICROTREND_MIN_BARS", 3))   # لازم آخر 3 إغلاقات تصاعدية

# Hotlist لالتقاط الرابحين بين دفعتين
HOT_N_H1                 = int(os.getenv("HOT_N_H1", 10))  # دمج أقوى 1h حسب 24h endpoint (تقريب)
HOT_BOOST                = float(os.getenv("HOT_BOOST", 2.5))

# وزن الزخم في ترتيب الغرفة
CURRENT_WEIGHT           = float(os.getenv("CURRENT_WEIGHT", 0.4))

# كاش حي
PRICE_TTL                = int(os.getenv("PRICE_TTL", 3))   # ثواني
CANDLE_TTL               = int(os.getenv("CANDLE_TTL", 15)) # ثواني

# =========================
# 📌 إعدادات عامة
# =========================
MAX_ROOM           = int(os.getenv("MAX_ROOM", 30))
BATCH_INTERVAL_SEC = int(os.getenv("BATCH_INTERVAL_SEC", 45))  # أسرع لالتقاط القفزات
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", 5))
ROOM_TTL_SEC       = int(os.getenv("ROOM_TTL_SEC", 2*3600))    # ساعتين

# =========================
# 🧠 التهيئة
# =========================
REDIS_URL = os.getenv("REDIS_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")

r = redis.from_url(REDIS_URL)
mom_hist = defaultdict(lambda: deque(maxlen=120))  # (ts, price) للزخم

NS = os.getenv("REDIS_NS", "room")
KEY_WATCH_SET   = f"{NS}:watch"
KEY_COIN_HASH   = lambda s: f"{NS}:coin:{s}"
KEY_INTERNAL_TOP= f"{NS}:internal_top"
KEY_GLOBAL_SCANS= f"{NS}:global_scans"
KEY_SCAN_COUNT  = lambda s: f"{NS}:scans:{s}"

# كاش داخلي خفيف
_last_price = {}  # sym -> (ts, price)
_last_cand  = {}  # sym -> (ts, candles)

# =========================
# 📈 أدوات مساعدة
# =========================
def pct(a,b): return ((a-b)/b*100.0) if b>0 else 0.0

def tg(msg):
    if not (BOT_TOKEN and CHAT_ID): return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg}, timeout=8)
    except: pass

def notify_saqr(text: str):
    if not SAQAR_WEBHOOK: return
    try:
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": text}}, timeout=8)
    except Exception as e:
        print("Saqr webhook error:", e)

def get_price_live(sym):
    now = time.time()
    ts, p = _last_price.get(sym, (0, 0.0))
    if now - ts <= PRICE_TTL and p > 0: return p
    try:
        j = requests.get(f"https://api.bitvavo.com/v2/ticker/price?market={sym}-EUR", timeout=4).json()
        p = float(j.get("price", 0) or 0.0)
    except: p = 0.0
    if p > 0: _last_price[sym] = (now, p)
    return p

def get_candles_live(sym, limit=60):
    now = time.time()
    ts, data = _last_cand.get(sym, (0, None))
    if data and now - ts <= CANDLE_TTL: return data
    try:
        url = f"https://api.bitvavo.com/v2/{sym}-EUR/candles?interval=1m&limit={limit}"
        data = requests.get(url, timeout=6).json()
    except: data = []
    _last_cand[sym] = (now, data)
    return data

def changes_from_candles(c):
    if not c: return None
    try:
        closes = [float(x[4]) for x in c]
        vols   = [float(x[5]) for x in c]
        if not closes: return None
        def safe(i): return pct(closes[-1], closes[-i]) if len(closes)>=i and closes[-i]>0 else 0.0
        base = (sum(vols[-16:-1])/15) if len(vols)>=16 else 0.0
        spike = (vols[-1]/base) if base>0 else 1.0
        micro_up = len(closes) >= MICROTREND_MIN_BARS and all(closes[-k] > closes[-k-1] for k in range(1, MICROTREND_MIN_BARS))
        return {"ch5": safe(6), "ch15": safe(16), "ch30": safe(31),
                "spike": spike, "close": closes[-1], "micro_up": micro_up, "closes": closes}
    except: return None

def calc_momentum(sym):
    dq = mom_hist.get(sym)
    if not dq or len(dq) < 3: return 0.0
    now, last = dq[-1]
    def rel(s):
        base = next((p for t,p in reversed(dq) if t <= now - s), None)
        return ((last-base)/base*100.0) if base else 0.0
    return 0.5*rel(5) + 0.3*rel(30) + 0.2*rel(60)

def room_members():
    out=[]
    for b in list(r.smembers(KEY_WATCH_SET)):
        s=b.decode()
        if r.exists(KEY_COIN_HASH(s)): out.append(s)
        else: r.srem(KEY_WATCH_SET, s)
    return out

def room_add(sym, price, pts, extra=None):
    mp = {
        "entry_price": f"{price}",
        "high": f"{price}",
        "pts": f"{pts}",
        "initial_pts": f"{pts}",
        "last_price": f"{price}",
        "last_demerit_ts": "0",
        "last_alert_ts": "0",
        "last_alert_price": "0",
        "ch5": "0", "spike": "1"
    }
    if extra: mp.update(extra)
    p = r.pipeline()
    p.hset(KEY_COIN_HASH(sym), mapping=mp)
    p.expire(KEY_COIN_HASH(sym), ROOM_TTL_SEC)
    p.sadd(KEY_WATCH_SET, sym)
    p.execute()

def apply_drop_demerit(sym, price, st):
    try:
        drop = pct(price, st["high"])
        if drop <= DROP_DEMERIT_PCT:
            last_b = r.hget(KEY_COIN_HASH(sym), "last_demerit_ts")
            last = int((last_b or b"0").decode()) if isinstance(last_b,(bytes,bytearray)) else int(last_b or 0)
            if time.time() - last >= DROP_DEMERIT_COOLDOWN:
                r.hincrbyfloat(KEY_COIN_HASH(sym), "pts", -abs(DROP_DEMERIT_POINTS))
                r.hset(KEY_COIN_HASH(sym), "last_demerit_ts", str(int(time.time())))
    except: pass

def check_remove_if_lost(sym, st):
    initial = st.get("initial_pts", 0.0)
    current = st.get("pts", 0.0)
    if initial > 0 and ((initial - current) / initial * 100.0) >= REMOVE_IF_LOST_PCT:
        r.delete(KEY_COIN_HASH(sym)); r.srem(KEY_WATCH_SET, sym)
        tg(f"⛔ حذف {sym} لخسارة ≥{REMOVE_IF_LOST_PCT:.0f}% من نقاطه")
        return True
    return False

def refresh_internal_top():
    rows=[]
    for s in room_members():
        d = r.hgetall(KEY_COIN_HASH(s))
        try: pts = float((d.get(b"pts") or b"0").decode())
        except: pts = 0.0
        score_now = pts + CURRENT_WEIGHT * calc_momentum(s)
        rows.append((s, score_now))
    rows.sort(key=lambda x: x[1], reverse=True)
    top = [s for s,_ in rows[:TRUST_TOP_N]]
    r.delete(KEY_INTERNAL_TOP)
    if top: r.sadd(KEY_INTERNAL_TOP, *top)
    return set(top)

# ========= Hotlist =========
def bitvavo_24h():
    try: return requests.get("https://api.bitvavo.com/v2/ticker/24h", timeout=8).json()
    except: return []

def hotlist_from_24h():
    rows = bitvavo_24h()
    def parse(row):
        m = row.get("market","")
        if not m.endswith("-EUR"): return None
        last = float(row.get("last",0) or 0.0)
        op   = float(row.get("open",0) or 0.0)
        ch = ((last-op)/op*100.0) if op>0 else 0.0
        return (m.replace("-EUR",""), ch)
    got = [p for p in (parse(x) for x in rows) if p]
    got.sort(key=lambda x:x[1], reverse=True)
    return set([s for s,_ in got[:HOT_N_H1]])

# =========================
# 📊 جمع 30 عملة (5m/15m/30m + مكافأة Top5 + Hotlist) وإعادة بناء الغرفة
# =========================
def batch_collect_loop():
    while True:
        try:
            markets = [m["market"].replace("-EUR","") for m in requests.get(
                "https://api.bitvavo.com/v2/markets", timeout=8).json() if m.get("market","").endswith("-EUR")]

            market_changes = {}
            for sym in markets:
                c = changes_from_candles(get_candles_live(sym, 60))
                if c: market_changes[sym] = c

            scored = {}
            for tf, w in (("ch5",0.55),("ch15",0.25),("ch30",0.20)):
                ranked = sorted(market_changes.items(), key=lambda kv: kv[1][tf], reverse=True)
                for idx, (sym, c) in enumerate(ranked):
                    add = w*max(0.0,c[tf]) + (1.0 if idx<5 else 0.0)
                    scored.setdefault(sym,[0.0,c])[0] += add

            # دمج Hotlist بالقوة
            for sym in hotlist_from_24h():
                if sym not in market_changes:
                    c = changes_from_candles(get_candles_live(sym, 60))
                    if c: market_changes[sym] = c
                if sym in market_changes:
                    scored.setdefault(sym,[0.0,market_changes[sym]])[0] += HOT_BOOST

            # اختيار Top30 جديد
            top30 = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)[:MAX_ROOM]
            new_set = set(sym for sym,_ in top30)

            # إعادة بناء الغرفة (شطب القديم)
            for s in room_members():
                if s not in new_set:
                    r.delete(KEY_COIN_HASH(s)); r.srem(KEY_WATCH_SET,s)

            for sym, (pts, c) in top30:
                if not r.exists(KEY_COIN_HASH(sym)):
                    room_add(sym, c["close"], pts, {"ch5": f"{c['ch5']}", "spike": f"{c['spike']}"})
                else:
                    r.hset(KEY_COIN_HASH(sym), mapping={
                        "pts": f"{pts}", "ch5": f"{c['ch5']}", "spike": f"{c['spike']}"})
        except Exception as e:
            print("batch_collect error:", e)
        time.sleep(BATCH_INTERVAL_SEC)

# =========================
# 🔍 مراقبة حيّة + إشعارات Top10 موثوق (تبريد/إعادة تسليح + Fast-Lane + مضاد وهم)
# =========================
def monitor_room_loop():
    while True:
        try:
            r.incr(KEY_GLOBAL_SCANS)
            glob_scans = int(r.get(KEY_GLOBAL_SCANS) or 0)
            top_set = refresh_internal_top()

            for sym in room_members():
                hb = r.hgetall(KEY_COIN_HASH(sym))
                def gf(k, dv=0.0):
                    b = hb.get(k.encode()) if isinstance(k,str) else hb.get(k)
                    try: return float(b.decode()) if isinstance(b,(bytes,bytearray)) else float(b or dv)
                    except: return dv
                st = {
                    "entry_price": gf("entry_price"), "high": gf("high"),
                    "pts": gf("pts"), "initial_pts": gf("initial_pts"),
                    "last_price": gf("last_price"), "ch5": gf("ch5"), "spike": gf("spike"),
                    "last_alert_ts": gf("last_alert_ts"), "last_alert_price": gf("last_alert_price")
                }

                # تحديث حيّ للقيم
                live_c = get_candles_live(sym, 60)
                live = changes_from_candles(live_c) if live_c else None
                if live:
                    price = live["close"]; ch5 = live["ch5"]; spike = live["spike"]; micro_up = live["micro_up"]
                else:
                    price = get_price_live(sym) or st["last_price"] or st["entry_price"]
                    ch5   = st["ch5"]; spike = st["spike"]; micro_up = False

                # حفظ وتحديث أعلى سعر + الزخم
                if price > 0:
                    r.hset(KEY_COIN_HASH(sym), mapping={"last_price": f"{price}", "ch5": f"{ch5}", "spike": f"{spike}"})
                    if price > st["high"]: r.hset(KEY_COIN_HASH(sym), "high", f"{price}")
                    mom_hist[sym].append((time.time(), price))

                # قواعد الحذف/الخصم
                st["high"] = float((r.hget(KEY_COIN_HASH(sym), "high") or b"0").decode() or 0)
                if check_remove_if_lost(sym, {"initial_pts": st["initial_pts"], "pts": float((r.hget(KEY_COIN_HASH(sym),"pts") or b"0").decode() or 0)}):
                    continue
                apply_drop_demerit(sym, price, st)

                # عدّاد دورات الغرفة
                scans = int(r.get(KEY_SCAN_COUNT(sym)) or 0) + 1
                r.set(KEY_SCAN_COUNT(sym), scans)

                # شروط عامة
                in_trusted = r.sismember(KEY_INTERNAL_TOP, sym)
                if glob_scans < GLOBAL_WARMUP_CYCLES: continue
                if scans < max(2, TRUST_WARMUP_SCANS): continue

                # تخفيف موثوق
                ch5_thr, spk_thr, move_thr = MIN_CH5_FOR_ALERT, MIN_SPIKE_FOR_ALERT, MIN_MOVE_FROM_ENTRY
                if in_trusted:
                    ch5_thr = max(0.0, ch5_thr + TRUST_CH5_DELTA)
                    spk_thr = max(1.0, spk_thr + TRUST_SPIKE_DELTA)
                    move_thr= max(0.0, move_thr + TRUST_MOVE_DELTA)

                move = pct(price, st["entry_price"])

                # Fast-Lane حقيقي (انفجار + حجم + microtrend صاعد)
                fast_ok = (ch5 >= FAST_CH5 and spike >= FAST_SPIKE and micro_up)

                # المسار العادي
                normal_ok = (ch5 >= ch5_thr and spike >= spk_thr and move >= move_thr and micro_up)

                if in_trusted and (fast_ok or normal_ok):
                    # تبريد + إعادة تسليح لمنع التكرار
                    last_ts    = st["last_alert_ts"]
                    last_price = st["last_alert_price"]
                    ok_time = (time.time() - last_ts) >= COOLDOWN_SEC
                    ok_move = (last_price == 0) or (price >= last_price * (1 + REARM_PCT/100.0))
                    if not (ok_time or ok_move):
                        continue

                    reason = "FastLane" if fast_ok else "Momentum+Volume"
                    msg = f"🚀 {sym} ({reason}) ch5={ch5:.2f}% spike={spike:.2f}× move={move:.2f}%"
                    tg(msg); notify_saqr(f"اشتري {sym}")
                    r.hset(KEY_COIN_HASH(sym), mapping={
                        "last_alert_ts": str(int(time.time())),
                        "last_alert_price": f"{price}"
                    })

        except Exception as e:
            print("monitor error:", e)
        time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 📜 أوامر تليجرام
# =========================
app = Flask(__name__)

@app.route("/", methods=["GET"])
def alive():
    return "Explosion-Hunter bot alive ✅", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    txt = (data.get("message", {}).get("text") or "").strip().lower()
    if txt in ("السجل","log"):
        rows=[]
        for s in room_members():
            pts = float(r.hget(KEY_COIN_HASH(s), "pts") or 0)
            rows.append((s, pts))
        rows.sort(key=lambda x:x[1], reverse=True)
        msg = "📊 غرفة Top10 (أعلى القائمة دائمًا):\n" + "\n".join([f"{i+1}. {sym} / {pts:.2f} نقاط" for i,(sym,pts) in enumerate(rows[:MAX_ROOM])])
        tg(msg)
    return "ok", 200

# =========================
# 🧹 مسح بيانات Redis عند التشغيل
# =========================
def reset_all():
    for key in r.keys(f"{NS}:*"):
        r.delete(key)
    print("🧹 تم مسح جميع بيانات Redis القديمة.")

# =========================
# ▶️ التشغيل
# =========================
if __name__ == "__main__":
    reset_all()
    Thread(target=batch_collect_loop, daemon=True).start()
    Thread(target=monitor_room_loop, daemon=True).start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)