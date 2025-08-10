# -*- coding: utf-8 -*-
import os, time, requests, redis
from threading import Thread
from flask import Flask, request
from waitress import serve

# ========= إعدادات سريعة قابلة للتعديل =========
NS                 = os.getenv("REDIS_NS", "mini")
MAX_ROOM_TTL_SEC   = int(os.getenv("ROOM_TTL_SEC", 2*3600))   # بقاء العملة ساعتين
COLLECT_EVERY_SEC  = int(os.getenv("COLLECT_EVERY_SEC", 180)) # كل 3 دقائق
SCAN_EVERY_SEC     = int(os.getenv("SCAN_EVERY_SEC", 5))      # تحديث سعر كل 5 ثواني
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT", -3.0))  # طرد لو ≤ -3% من سعر الدخول
ALERT_TOP_N        = int(os.getenv("ALERT_TOP_N", 10))        # إشعارات فقط من أعلى N (10 أو 20)
MIN_MOVE_ALERT     = float(os.getenv("MIN_MOVE_ALERT", 1.0))  # لازم يكون +1% من الدخول
MIN_CH5_ALERT      = float(os.getenv("MIN_CH5_ALERT", 0.3))   # تقوية بسيطة
MIN_SPIKE_ALERT    = float(os.getenv("MIN_SPIKE_ALERT", 1.2)) # حجم لحظي
ALLOW_REPEAT       = int(os.getenv("ALLOW_REPEAT", 0))        # 0= بلا تكرار، 1= يسمح إذا حقق قمة جديدة
REARM_PCT          = float(os.getenv("REARM_PCT", 1.5))       # لو ALLOW_REPEAT=1 لازم يعدّي آخر إشعار بـ %
BOT_TOKEN          = os.getenv("BOT_TOKEN"); CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK      = os.getenv("SAQAR_WEBHOOK")

# ========= مفاتيح Redis =========
r = redis.from_url(os.getenv("REDIS_URL"))
KEY_WATCH   = f"{NS}:watch"
KEY_COIN    = lambda s: f"{NS}:c:{s}"

# ========= أدوات بسيطة =========
def pct(a,b): return ((a-b)/b*100.0) if b>0 else 0.0
def tg(msg):
    if not (BOT_TOKEN and CHAT_ID): return
    try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                       data={"chat_id": CHAT_ID, "text": msg}, timeout=8)
    except: pass
def saqr(txt):
    if not SAQAR_WEBHOOK: return
    try: requests.post(SAQAR_WEBHOOK, json={"message":{"text":txt}}, timeout=6)
    except: pass

def markets_eur():
    try:
        arr=requests.get("https://api.bitvavo.com/v2/markets",timeout=8).json()
        return [m["market"].replace("-EUR","") for m in arr if m.get("market","").endswith("-EUR")]
    except: return []

def candles_1m(sym, limit=240):
    try:
        return requests.get(f"https://api.bitvavo.com/v2/{sym}-EUR/candles?interval=1m&limit={limit}",timeout=6).json()
    except: return []

def changes_from_1m(c):
    if not isinstance(c,list) or len(c)<2: return None
    closes=[float(x[4]) for x in c]; vols=[float(x[5]) for x in c]
    def safe(n): return pct(closes[-1], closes[-n]) if len(closes)>=n and closes[-n]>0 else 0.0
    base=(sum(vols[-16:-1])/15) if len(vols)>=16 else 0.0
    return {
        "last": closes[-1],
        "ch5": safe(6), "ch15": safe(16), "ch30": safe(31),
        "ch60": safe(61), "ch240": safe(241),
        "spike": (vols[-1]/base) if base>0 else 1.0
    }

def price(sym):
    try:
        j=requests.get(f"https://api.bitvavo.com/v2/ticker/price?market={sym}-EUR",timeout=4).json()
        return float(j.get("price",0) or 0.0)
    except: return 0.0

def room_members():
    syms=list(r.smembers(KEY_WATCH))
    out=[]
    for b in syms:
        s=b.decode()
        if r.exists(KEY_COIN(s)): out.append(s)
        else: r.srem(KEY_WATCH,s)
    return out

# ========= 1) مسح كامل عند التشغيل =========
def reset_all():
    for k in r.keys(f"{NS}:*"): r.delete(k)
    print("🧹 Redis cleared.")

# ========= 2) تجميع Top5 لكل فريم ودمجهم =========
def collect_loop():
    while True:
        try:
            syms=markets_eur()
            score={}
            for sym in syms:
                d=changes_from_1m(candles_1m(sym, 241))
                if not d: continue
                score[sym]=d
            # ترتيب لكل فريم واختيار Top5
            picks=set()
            for tf in ["ch5","ch15","ch30","ch60","ch240"]:
                top=sorted(score.items(), key=lambda kv: kv[1].get(tf,0), reverse=True)[:5]
                for s,_ in top: picks.add(s)
            # دخول الغرفة (بدون طرد إجباري، فقط TTL ساعتين)
            now=int(time.time())
            for sym in picks:
                if not r.exists(KEY_COIN(sym)):
                    px=score[sym]["last"]
                    r.hset(KEY_COIN(sym), mapping={
                        "entry": f"{px}", "t": str(now),
                        "last": f"{px}", "alerted": "0",
                        "high_move": "0", "last_alert_price": "0",
                    })
                    r.expire(KEY_COIN(sym), MAX_ROOM_TTL_SEC)
                    r.sadd(KEY_WATCH, sym)
        except Exception as e:
            print("collect error:", e)
        time.sleep(COLLECT_EVERY_SEC)

# ========= 3) مراقبة 5 ثواني + طرد -3% + ترتيب + إشعارات أعلى N =========
def monitor_loop():
    while True:
        try:
            members=room_members()
            rows=[]
            # تحديث وحساب الـ move من الدخول
            for sym in members:
                h=r.hgetall(KEY_COIN(sym))
                entry=float((h.get(b"entry") or b"0").decode() or 0)
                last=float((h.get(b"last") or b"0").decode() or 0)
                alerted=int((h.get(b"alerted") or b"0").decode() or 0)
                lap=float((h.get(b"last_alert_price") or b"0").decode() or 0)
                # لقطة حية صغيرة (سعر + ch5/spike)
                c=changes_from_1m(candles_1m(sym, 16))
                px=c["last"] if c else price(sym)
                if px<=0: px=last or entry
                r.hset(KEY_COIN(sym),"last", f"{px}")
                move=pct(px, entry)
                ch5=c["ch5"] if c else 0.0
                spk=c["spike"] if c else 1.0
                rows.append((sym, move, ch5, spk, alerted, lap, entry, px))
            # طرد -3% واضح
            for sym,move,_,_,_,_,entry,px in rows:
                if move <= STOP_LOSS_PCT:
                    r.delete(KEY_COIN(sym)); r.srem(KEY_WATCH,sym)
            # ترتيب داخلي حسب move
            rows=[row for row in rows if r.exists(KEY_COIN(row[0]))]
            rows.sort(key=lambda x: x[1], reverse=True)
            top_rows=rows[:ALERT_TOP_N]

            # إشعار مرة واحدة (أو عند قمة جديدة فقط)
            for sym,move,ch5,spk,alerted,lap,entry,px in top_rows:
                if move < MIN_MOVE_ALERT or ch5 < MIN_CH5_ALERT or spk < MIN_SPIKE_ALERT:
                    continue
                if not alerted:
                    msg=f"🚀 {sym} Top{ALERT_TOP_N} move={move:.2f}% ch5={ch5:.2f}% spike={spk:.2f}×"
                    tg(msg); saqr(f"اشتري {sym}")
                    r.hset(KEY_COIN(sym), mapping={"alerted":"1","last_alert_price":f"{px}"})
                elif ALLOW_REPEAT:
                    need=lap*(1+REARM_PCT/100.0) if lap>0 else entry*(1+REARM_PCT/100.0)
                    if px>=need:
                        msg=f"🚀 {sym} New High move={move:.2f}%"
                        tg(msg); saqr(f"اشتري {sym}")
                        r.hset(KEY_COIN(sym), "last_alert_price", f"{px}")
        except Exception as e:
            print("monitor error:", e)
        time.sleep(SCAN_EVERY_SEC)

# ========= 4) /السجل لعرض الترتيب =========
app=Flask(__name__)
@app.route("/", methods=["GET"])
def alive(): return "Mini top-room bot ✅",200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    txt  = (data.get("message",{}).get("text") or "").strip().lower()
    if txt in ("السجل","log"):
        rows=[]
        for sym in room_members():
            h=r.hgetall(KEY_COIN(sym))
            entry=float((h.get(b"entry") or b"0").decode() or 0)
            last =float((h.get(b"last") or b"0").decode() or 0)
            rows.append((sym, pct(last,entry)))
        rows.sort(key=lambda x:x[1], reverse=True)
        tg("📊 غرفة Top:\n"+"\n".join([f"{i+1}. {s} / {m:.2f}%" for i,(s,m) in enumerate(rows[:40])]))
    return "ok",200

# ========= تشغيل =========
if __name__=="__main__":
    # مسح كامل
    for k in r.keys(f"{NS}:*"): r.delete(k)
    print("🧹 cleared & starting …")
    Thread(target=collect_loop, daemon=True).start()
    Thread(target=monitor_loop, daemon=True).start()
    from waitress import serve
    port=int(os.getenv("PORT",5000))
    try:
        serve(app, host="0.0.0.0", port=port)
    except:
        app.run(host="0.0.0.0", port=port)