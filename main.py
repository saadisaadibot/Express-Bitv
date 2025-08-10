# -*- coding: utf-8 -*-
import os, time, json, requests, redis
from collections import deque, defaultdict
from threading import Thread
from flask import Flask, request

# =========================
# 📌 إعدادات الإشعار
# =========================
TRUST_TOP_N              = int(os.getenv("TRUST_TOP_N", 10))
TRUST_WARMUP_SCANS       = int(os.getenv("TRUST_WARMUP_SCANS", 3))
TRUST_CH5_DELTA          = float(os.getenv("TRUST_CH5_DELTA", -0.3))
TRUST_SPIKE_DELTA        = float(os.getenv("TRUST_SPIKE_DELTA", -0.2))
TRUST_MOVE_DELTA         = float(os.getenv("TRUST_MOVE_DELTA", -0.1))
GLOBAL_WARMUP_CYCLES     = int(os.getenv("GLOBAL_WARMUP_CYCLES", 2))
DROP_DEMERIT_PCT         = float(os.getenv("DROP_DEMERIT_PCT", -2.0))
DROP_DEMERIT_POINTS      = float(os.getenv("DROP_DEMERIT_POINTS", 3.0))
DROP_DEMERIT_COOLDOWN    = int(os.getenv("DROP_DEMERIT_COOLDOWN", 30))
MIN_CH5_FOR_ALERT        = 0.7
MIN_SPIKE_FOR_ALERT      = 1.1
MIN_MOVE_FROM_ENTRY      = 0.25
REMOVE_IF_LOST_PCT       = float(os.getenv("REMOVE_IF_LOST_PCT", 50.0))  # حذف إذا فقد 50% أو أكثر من نقاطه

# =========================
# 📌 إعدادات عامة
# =========================
MAX_ROOM           = 30
BATCH_INTERVAL_SEC = 90
SCAN_INTERVAL_SEC  = 5
ROOM_TTL_SEC       = 2 * 3600   # ساعتين

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
def pct(a,b): return ((a-b)/b*100.0) if b>0 else 0.0
def tg(msg): 
    if BOT_TOKEN and CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                           data={"chat_id":CHAT_ID,"text":msg})
        except: pass

def get_candles(sym,limit=60):
    try:
        url=f"https://api.bitvavo.com/v2/{sym}-EUR/candles?interval=1m&limit={limit}"
        return requests.get(url,timeout=6).json()
    except: return []

def changes_from_candles(c):
    if not c: return None
    closes=[float(x[4]) for x in c]
    if not closes: return None
    safe=lambda i: pct(closes[-1],closes[-i]) if len(closes)>=i and closes[-i]>0 else 0
    return {"ch5":safe(6),"ch15":safe(16),"ch30":safe(31),
            "spike": (float(c[-1][5])/ (sum(float(x[5]) for x in c[-16:-1])/15)) if len(c)>=16 else 1.0,
            "close": closes[-1]}

def calc_momentum(sym):
    dq = mom_hist.get(sym)
    if not dq or len(dq)<3: return 0.0
    now,last = dq[-1]
    def rel(s):
        base = next((p for t,p in reversed(dq) if t<=now-s), None)
        return ((last-base)/base*100) if base else 0.0
    return 0.5*rel(5)+0.3*rel(30)+0.2*rel(60)

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
    if initial > 0 and ((initial - current) / initial * 100) >= REMOVE_IF_LOST_PCT:
        r.delete(KEY_COIN_HASH(sym))
        r.srem(KEY_WATCH_SET, sym)
        tg(f"⛔ حذف {sym} لخسارته {REMOVE_IF_LOST_PCT}% من نقاطه")
        return True
    return False

# =========================
# 📊 جمع العملات (مع مكافأة Top5)
# =========================
def batch_collect():
    try:
        markets=[m["market"].replace("-EUR","") for m in requests.get("https://api.bitvavo.com/v2/markets").json() if m["market"].endswith("-EUR")]
        scored=[]
        market_changes = {}
        for sym in markets:
            c=changes_from_candles(get_candles(sym))
            if c: market_changes[sym] = c
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

# =========================
# 🔍 المراقبة
# =========================
def monitor_room():
    while True:
        try:
            r.incr(KEY_GLOBAL_SCANS)
            glob_scans=int(r.get(KEY_GLOBAL_SCANS) or 0)
            top_set=refresh_internal_top()
            for sym in room_members():
                st_json=r.hgetall(KEY_COIN_HASH(sym))
                st={k.decode():float(v.decode()) for k,v in st_json.items() if v}
                price=st.get("last_price", st["entry_price"])
                # حذف إذا فقد 50% من النقاط
                if check_remove_if_lost(sym,st):
                    continue
                apply_drop_demerit(sym,price,st)
                scans=int(r.get(KEY_SCAN_COUNT(sym)) or 0)+1
                r.set(KEY_SCAN_COUNT(sym),scans)
                in_trusted = sym in top_set
                if glob_scans<GLOBAL_WARMUP_CYCLES: continue
                if scans<max(2,TRUST_WARMUP_SCANS): continue
                ch5_thr,spk_thr,move_thr = MIN_CH5_FOR_ALERT,MIN_SPIKE_FOR_ALERT,MIN_MOVE_FROM_ENTRY
                if in_trusted:
                    ch5_thr+=TRUST_CH5_DELTA
                    spk_thr+=TRUST_SPIKE_DELTA
                    move_thr+=TRUST_MOVE_DELTA
                ch5,spike,move = st.get("ch5",0), st.get("spike",1), pct(price,st["entry_price"])
                if ch5>=ch5_thr and spike>=spk_thr and move>=move_thr and in_trusted:
                    tg(f"🚀 {sym} موثوق ch5={ch5:.2f}% spike={spike:.2f} move={move:.2f}%")
            time.sleep(SCAN_INTERVAL_SEC)
        except Exception as e:
            print("monitor error:",e); time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 📜 أوامر تليجرام
# =========================
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json() or {}
    txt = (data.get("message", {}).get("text") or "").strip().lower()
    if txt in ("السجل","log"):
        rows=[]
        for s in room_members():
            pts=float(r.hget(KEY_COIN_HASH(s),"pts") or 0)
            rows.append((s,pts))
        rows.sort(key=lambda x:x[1],reverse=True)
        msg="\n".join([f"{i+1}. {sym} / {pts:.2f} نقاط" for i,(sym,pts) in enumerate(rows)])
        tg(msg)
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
    Thread(target=lambda: [batch_collect() or time.sleep(BATCH_INTERVAL_SEC) for _ in iter(int,1)],daemon=True).start()
    Thread(target=monitor_room,daemon=True).start()
    port=int(os.getenv("PORT",5000))
    app.run(host="0.0.0.0",port=port)