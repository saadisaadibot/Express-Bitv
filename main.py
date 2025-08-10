# -*- coding: utf-8 -*-
import os, time, json, requests, redis
from collections import deque, defaultdict
from threading import Thread
from flask import Flask, request

# =========================
# 📌 إعدادات الإشعار / النقاط (كما كانت)
# =========================
# 📌 إعدادات يدوية مباشرة
TRUST_TOP_N              = 12
TRUST_WARMUP_SCANS       = 2
TRUST_CH5_DELTA          = -0.6
TRUST_SPIKE_DELTA        = -0.3
TRUST_MOVE_DELTA         = -0.1
GLOBAL_WARMUP_CYCLES     = 1
DROP_DEMERIT_PCT         = -3.0
DROP_DEMERIT_POINTS      = 2.0
DROP_DEMERIT_COOLDOWN    = 45
MIN_CH5_FOR_ALERT        = 0.4
MIN_SPIKE_FOR_ALERT      = 1.0
MIN_MOVE_FROM_ENTRY      = 0.10
REMOVE_IF_LOST_PCT       = 60.0
# =========================
# 📌 إعدادات عامة
# =========================
MAX_ROOM           = 30
BATCH_INTERVAL_SEC = 90
SCAN_INTERVAL_SEC  = 5
ROOM_TTL_SEC       = 2 * 3600   # ساعتين
BV                 = "https://api.bitvavo.com/v2"

# =========================
# 🧠 التهيئة
# =========================
REDIS_URL = os.getenv("REDIS_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
r = redis.from_url(REDIS_URL)
mom_hist = defaultdict(lambda: deque(maxlen=120))
NS = "room"
KEY_WATCH_SET = f"{NS}:watch"
KEY_COIN_HASH = lambda s: f"{NS}:coin:{s}"
KEY_INTERNAL_TOP = f"{NS}:internal_top"
KEY_GLOBAL_SCANS = f"{NS}:global_scans"
KEY_SCAN_COUNT = lambda s: f"{NS}:scans:{s}"

# =========================
# 📈 أدوات مساعدة
# =========================
def pct(a,b): 
    try:
        return ((a-b)/b*100.0) if b>0 else 0.0
    except: 
        return 0.0

def tg(msg, chat_id=None): 
    cid = chat_id or CHAT_ID
    if not (BOT_TOKEN and cid):
        print("TG:", msg); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": cid, "text": msg}, timeout=10)
    except: 
        pass

def get_candles(sym, limit=60, start=None, end=None):
    """شموع 1m من Bitvavo (تصحيح الاندبوينت لتفادي 404)."""
    try:
        params = {"market": f"{sym}-EUR", "interval": "1m", "limit": limit}
        if start: params["start"] = str(start)
        if end:   params["end"]   = str(end)
        rqt = requests.get(f"{BV}/candles", params=params, timeout=8)
        if rqt.status_code != 200:
            return []
        data = rqt.json()
        return data if isinstance(data, list) else []
    except:
        return []

def get_price(sym):
    """سعر حي خفيف."""
    try:
        rqt = requests.get(f"{BV}/ticker/price", params={"market": f"{sym}-EUR"}, timeout=6)
        if rqt.status_code != 200: 
            return None
        return float(rqt.json().get("price", 0))
    except:
        return None

def changes_from_candles(c):
    if not c: return None
    try:
        closes=[float(x[4]) for x in c]
        vols=[float(x[5]) for x in c]
    except:
        return None
    if not closes: return None
    def safe(i): 
        return pct(closes[-1],closes[-i]) if len(closes)>=i and closes[-i]>0 else 0.0
    v15 = (sum(vols[-16:-1])/15.0) if len(vols)>=16 else (sum(vols[-5:-1])/max(1,len(vols[-5:-1])))
    spike = (vols[-1]/v15) if v15>0 else 1.0
    return {"ch5":safe(6),"ch15":safe(16),"ch30":safe(31),"spike":spike,"close":closes[-1]}

def calc_momentum(sym):
    dq = mom_hist.get(sym)
    if not dq or len(dq)<3: return 0.0
    now,last = dq[-1]
    def rel(s):
        base = next((p for t,p in reversed(dq) if t<=now-s), None)
        return ((last-base)/base*100) if base else 0.0
    return 0.5*rel(5)+0.3*rel(30)+0.2*rel(60)

# =========================
# 🧱 إدارة الغرفة / نقاط
# =========================
def refresh_internal_top():
    rows=[]
    for s in room_members():
        d=r.hgetall(KEY_COIN_HASH(s))
        pts=float(d.get(b"pts",b"0") or 0)
        score_now=pts+0.4*calc_momentum(s)
        rows.append((s,score_now))
    rows.sort(key=lambda x:x[1],reverse=True)
    top=[s for s,_ in rows[:TRUST_TOP_N]]
    r.delete(KEY_INTERNAL_TOP)
    if top: r.sadd(KEY_INTERNAL_TOP,*top)
    return set(top)

def room_members():
    syms=list(r.smembers(KEY_WATCH_SET))
    out=[]
    for b in syms:
        s=b.decode()
        if r.exists(KEY_COIN_HASH(s)): out.append(s)
        else: r.srem(KEY_WATCH_SET,s)
    return out

def room_add(sym,price,pts):
    p = r.pipeline()
    p.hset(KEY_COIN_HASH(sym),mapping={
        "entry_price":price,
        "high":price,
        "pts":pts,
        "last_price":price,
        "last_demerit_ts":0,
        "initial_pts":pts
    })
    p.expire(KEY_COIN_HASH(sym), ROOM_TTL_SEC)
    p.sadd(KEY_WATCH_SET,sym)
    p.execute()

def apply_drop_demerit(sym,price,st):
    drop=pct(price,st["high"])
    if drop<=DROP_DEMERIT_PCT:
        last=int((r.hget(KEY_COIN_HASH(sym),"last_demerit_ts") or b"0").decode())
        if time.time()-last>=DROP_DEMERIT_COOLDOWN:
            r.hincrbyfloat(KEY_COIN_HASH(sym),"pts",-abs(DROP_DEMERIT_POINTS))
            r.hset(KEY_COIN_HASH(sym),"last_demerit_ts",str(int(time.time())))

def check_remove_if_lost(sym,st):
    initial = st.get("initial_pts", 0)
    current = st.get("pts", 0)
    if initial > 0 and ((initial - current) / max(1e-9,initial) * 100) >= REMOVE_IF_LOST_PCT:
        r.delete(KEY_COIN_HASH(sym))
        r.srem(KEY_WATCH_SET, sym)
        tg(f"⛔ حذف {sym} لخسارته {REMOVE_IF_LOST_PCT}% من نقاطه")
        return True
    return False

# =========================
# 📊 جمع العملات (كما كانت + تصحيح الشموع)
# =========================
def batch_collect_once():
    try:
        markets=[m["market"].replace("-EUR","") for m in requests.get(f"{BV}/markets", timeout=15).json() if m.get("market","").endswith("-EUR")]
        scored=[]
        market_changes = {}
        for sym in markets:
            c = get_candles(sym, limit=60)
            if not c: 
                continue
            cc = changes_from_candles(c)
            if cc: 
                market_changes[sym] = cc
        for tf, weight in [("ch5",0.5),("ch15",0.3),("ch30",0.2)]:
            ranked = sorted(market_changes.items(), key=lambda kv: kv[1][tf], reverse=True)
            for idx, (sym, c) in enumerate(ranked):
                pts = weight * max(0, c[tf])
                if idx < 5:
                    pts += 1.0
                scored.append((sym, pts, c))
        final_scores = {}
        for sym, pts, c in scored:
            if sym not in final_scores:
                final_scores[sym] = [0, c]
            final_scores[sym][0] += pts
        sorted_final = sorted(final_scores.items(), key=lambda kv: kv[1][0], reverse=True)
        for sym, (pts, c) in sorted_final[:MAX_ROOM]:
            if not r.exists(KEY_COIN_HASH(sym)):
                room_add(sym,c["close"],pts)
            else:
                r.hset(KEY_COIN_HASH(sym),"pts",pts)
    except Exception as e:
        print("batch_collect error:",e)

def batch_collect_loop():
    while True:
        batch_collect_once()
        time.sleep(BATCH_INTERVAL_SEC)

# =========================
# 🔍 المراقبة وتحديث السعر الحي
# =========================
def monitor_room():
    while True:
        try:
            r.incr(KEY_GLOBAL_SCANS)
            glob_scans=int(r.get(KEY_GLOBAL_SCANS) or 0)
            top_set=refresh_internal_top()
            for sym in room_members():
                # سعر حي وتحديث high/last
                live = get_price(sym)
                if live is not None:
                    r.hset(KEY_COIN_HASH(sym),"last_price", live)
                    # حدّث القمة
                    try:
                        cur_high = float((r.hget(KEY_COIN_HASH(sym),"high") or b"0").decode())
                        if live > cur_high: r.hset(KEY_COIN_HASH(sym),"high", live)
                    except: pass

                st_json=r.hgetall(KEY_COIN_HASH(sym))
                st={k.decode():float(v.decode()) for k,v in st_json.items() if v}
                price=st.get("last_price", st.get("entry_price",0.0))
                if not price: 
                    continue

                # حذف إذا فقد 50% من النقاط
                if check_remove_if_lost(sym,st):
                    continue

                apply_drop_demerit(sym,price,st)

                scans=int(r.get(KEY_SCAN_COUNT(sym)) or 0)+1
                r.set(KEY_SCAN_COUNT(sym),scans)

                in_trusted = sym in top_set
                if glob_scans<GLOBAL_WARMUP_CYCLES: continue
                if scans<max(2,TRUST_WARMUP_SCANS): continue

                # حساب ch5/spike محدث كل دورة خفيفة (من شموع 11 دقيقة)
                c = get_candles(sym, limit=16)
                cchg = changes_from_candles(c) if c else None
                ch5  = cchg["ch5"] if cchg else 0
                spike= cchg["spike"] if cchg else 1
                move = pct(price, st.get("entry_price", price))

                ch5_thr,spk_thr,move_thr = MIN_CH5_FOR_ALERT,MIN_SPIKE_FOR_ALERT,MIN_MOVE_FROM_ENTRY
                if in_trusted:
                    ch5_thr+=TRUST_CH5_DELTA
                    spk_thr+=TRUST_SPIKE_DELTA
                    move_thr+=TRUST_MOVE_DELTA

                if ch5>=ch5_thr and spike>=spk_thr and move>=move_thr and in_trusted:
                    tg(f"🚀 {sym} موثوق ch5={ch5:.2f}% spike={spike:.2f} move={move:.2f}%")
            time.sleep(SCAN_INTERVAL_SEC)
        except Exception as e:
            print("monitor error:",e); time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 🧪 فلترة “ترند نظيف”: HL + بدون كسر + صعود 1h
# =========================
UP_CH1H_MIN      = float(os.getenv("UP_CH1H_MIN", 0.5))
HL_MIN_GAP_PCT   = float(os.getenv("HL_MIN_GAP_PCT", 0.3))
MAX_RED_CANDLE   = float(os.getenv("MAX_RED_CANDLE", -2.0))
SWING_DEPTH      = int(os.getenv("SWING_DEPTH", 3))
MIN_ABOVE_L2_PCT = float(os.getenv("MIN_ABOVE_L2_PCT", 0.5))

def _is_local_min(closes, i, depth):
    left  = all(closes[i] <= closes[k] for k in range(max(0, i-depth), i))
    right = all(closes[i] <= closes[k] for k in range(i+1, min(len(closes), i+1+depth)))
    return left and right

def _last_two_swings_low(closes, depth):
    lows = []
    for i in range(depth, len(closes)-depth):
        if _is_local_min(closes, i, depth):
            lows.append((i, closes[i]))
    return lows[-2:] if len(lows) >= 2 else None

def is_strong_clean_uptrend(sym):
    try:
        cs = get_candles(sym, limit=90)
        if len(cs) < 30: return False
        closes = [float(c[4]) for c in cs]
        p_now  = closes[-1]

        base = closes[-61] if len(closes) >= 62 else closes[0]
        ch1h = (p_now - base) / base * 100.0
        if ch1h < UP_CH1H_MIN: 
            return False

        start_idx = max(1, len(closes)-61)
        for i in range(start_idx, len(closes)):
            step = (closes[i] - closes[i-1]) / closes[i-1] * 100.0
            if step <= MAX_RED_CANDLE:
                return False

        swings = _last_two_swings_low(closes, SWING_DEPTH)
        if not swings: return False
        (i1, L1), (i2, L2) = swings
        if (L2 - L1) / L1 * 100.0 < HL_MIN_GAP_PCT:
            return False
        if any(p < L2 for p in closes[i2:]):  # لا كسر بعد تكوّن L2
            return False
        if (p_now - L2) / L2 * 100.0 < MIN_ABOVE_L2_PCT:
            return False
        return True
    except:
        return False

# =========================
# 📜 أوامر تليجرام (Webhook ثابت)
# =========================
app = Flask(__name__)

def _extract_update_fields(update: dict):
    msg = update.get("message") or update.get("edited_message") \
          or update.get("channel_post") or update.get("edited_channel_post") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    return text, chat_id

def _normalize_cmd(text: str):
    text = text.replace("\u200f", "").replace("\u200e", "").strip()
    if not text: return ""
    first = text.split()[0]
    if "@" in first:
        first = first.split("@", 1)[0]
    return first.lower()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    raw_text, chat_id = _extract_update_fields(data)
    cmd = _normalize_cmd(raw_text)

    if cmd in ("/start","ابدأ","start"):
        tg("✅ الصيّاد يعمل. أرسل /السجل لعرض التوب 10 (ترند نظيف).", chat_id)
    elif cmd in ("/السجل","السجل","/log","log","/snapshot","snapshot"):
        rows=[]
        for s in room_members():
            pts=float((r.hget(KEY_COIN_HASH(s),"pts") or b"0").decode())
            rows.append((s,pts))
        rows.sort(key=lambda x:x[1],reverse=True)

        # فلترة الترند النظيف
        out=[]
        for i,(sym,pts) in enumerate(rows,1):
            if is_strong_clean_uptrend(sym):
                price = r.hget(KEY_COIN_HASH(sym),"last_price")
                price = float(price.decode()) if price else 0.0
                out.append(f"{len(out)+1:02d}. ✅ {sym}  نقاط {pts:.2f}  | الآن {price:.6f}€")
            if len(out) >= 10: 
                break
        if not out:
            tg("⚠️ لا توجد حالياً عملات مطابقة لشرط: HL + بدون كسر + صعود آخر ساعة.", chat_id)
        else:
            tg("📊 توب 10 (ترند نظيف دون كسر آخر ساعة):\n" + "\n".join(out), chat_id)
    return "ok", 200

# =========================
# 🧹 مسح البيانات القديمة عند التشغيل
# =========================
def reset_all():
    for key in r.keys(f"{NS}:*"):
        r.delete(key)
    print("🧹 تم مسح جميع بيانات Redis القديمة.")

# =========================
# ▶️ التشغيل
# =========================
if __name__=="__main__":
    reset_all()
    Thread(target=batch_collect_loop, daemon=True).start()
    Thread(target=monitor_room, daemon=True).start()
    port=int(os.getenv("PORT",8080))
    app.run(host="0.0.0.0", port=port)