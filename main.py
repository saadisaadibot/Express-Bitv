# -*- coding: utf-8 -*-
import os, time, json, math, requests, redis
from collections import deque, defaultdict
from threading import Thread
from datetime import datetime, timezone
from flask import Flask, request

# =========================
# ⚙️ إعدادات عامة قابلة للتعديل
# =========================
TOP_N                    = int(os.getenv("TOP_N", 10))
ALPHA_EMA                = float(os.getenv("ALPHA_EMA", 0.35))     # تنعيم الدرجة
ENTRY_TH                 = float(os.getenv("ENTRY_TH", 1.2))       # عتبة دخول (مرتين)
EXIT_TH                  = float(os.getenv("EXIT_TH", 0.4))        # عتبة خروج (مرتين)
CONFIRM_UP_PASSES        = int(os.getenv("CONFIRM_UP_PASSES", 2))
CONFIRM_DOWN_PASSES      = int(os.getenv("CONFIRM_DOWN_PASSES", 2))
REPLACEMENT_GAP          = float(os.getenv("REPLACEMENT_GAP", 0.6))# فرق مطلوب لاستبدال أضعف عضو
PROFILE_PERIOD_SEC       = int(os.getenv("PROFILE_PERIOD_SEC", 3600)) # كل ساعة
REBUILD_PERIOD_SEC       = int(os.getenv("REBUILD_PERIOD_SEC", 600))  # كل 10 دقائق تقييم/ترتيب
SCAN_INTERVAL_SEC        = int(os.getenv("SCAN_INTERVAL_SEC", 30))    # تحديث درجات مستمرة
SWAP_SOFT_LIMIT          = int(os.getenv("SWAP_SOFT_LIMIT", 2))       # أقصى تبديلات/20 دقيقة
SWAP_WINDOW_SEC          = int(os.getenv("SWAP_WINDOW_SEC", 1200))    # 20 دقيقة
GLOBAL_SWAP_COOLDOWN_S   = int(os.getenv("GLOBAL_SWAP_COOLDOWN_S", 600)) # 10 دقائق بعد تبديل
EMERGENCY_CH10           = float(os.getenv("EMERGENCY_CH10", -3.5))  # خروج طارئ: 10m ≤ هذا
MIN_PRICE_EUR            = float(os.getenv("MIN_PRICE_EUR", 0.0005))
CLEAR_ON_START           = int(os.getenv("CLEAR_ON_START", 0))

# فلتر /السجل (ترند نظيف)
UP_CH1H_MIN      = float(os.getenv("UP_CH1H_MIN", 0.5))
HL_MIN_GAP_PCT   = float(os.getenv("HL_MIN_GAP_PCT", 0.3))
MAX_RED_CANDLE   = float(os.getenv("MAX_RED_CANDLE", -2.0))
SWING_DEPTH      = int(os.getenv("SWING_DEPTH", 3))
MIN_ABOVE_L2_PCT = float(os.getenv("MIN_ABOVE_L2_PCT", 0.5))

# Bitvavo + Telegram
BV        = "https://api.bitvavo.com/v2"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

# Redis
r = redis.from_url(os.getenv("REDIS_URL"))
NS = "hunter"
KEY_STATE          = lambda s: f"{NS}:state:{s}"   # score_smooth, up_streak, down_streak, in_room, last_price, high2h
KEY_PROFILE        = lambda s: f"{NS}:profile:{s}" # mu15, sd15, mu60, sd60, muvol, sdvol
KEY_ROOM           = f"{NS}:room"                  # مجموعة أعضاء الغرفة
KEY_SWAP_BUCKET    = f"{NS}:swap_bucket"           # عداد تبديلات ضمن نافذة
KEY_SWAP_COOLDOWN  = f"{NS}:swap_cooldown"
KEY_LAST_REBUILD   = f"{NS}:last_rebuild"

# =========================
# 🧰 أدوات
# =========================
def pct(a,b): 
    try:
        return ( (a-b)/b*100.0 ) if b>0 else 0.0
    except: 
        return 0.0

def now_ms(): 
    return int(datetime.now(timezone.utc).timestamp()*1000)

def send_msg(text, chat_id=None):
    cid = chat_id or CHAT_ID
    if not BOT_TOKEN or not cid:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": cid, "text": text}, timeout=10)
    except: pass

def get_markets_eur():
    try:
        resp = requests.get(f"{BV}/markets", timeout=15)
        resp.raise_for_status()
        out=[]
        for m in resp.json():
            if m.get("quote")=="EUR" and m.get("status")=="trading":
                out.append(m["market"])
        return out
    except Exception as e:
        print("markets err:", e); return []

def get_candles_1m(market, limit=1200, start=None, end=None):
    try:
        params={"market":market,"interval":"1m","limit":limit}
        if start: params["start"]=str(start)
        if end: params["end"]=str(end)
        rqt=requests.get(f"{BV}/candles", params=params, timeout=15)
        if rqt.status_code!=200: return []
        data=rqt.json()
        return data if isinstance(data,list) else []
    except: return []

def get_price(market):
    try:
        rqt=requests.get(f"{BV}/ticker/price", params={"market":market}, timeout=8)
        if rqt.status_code!=200: return None
        return float(rqt.json().get("price",0))
    except: return None

# =========================
# 🧠 بروفايل 24h (مرجع السلوك)
# =========================
def build_profile(sym_market):
    # نجلب 24h 1m (حتى 1440 شمعة)
    end = now_ms()
    start = end - 24*60*60*1000
    cs = get_candles_1m(sym_market, limit=1440, start=start, end=end)
    if len(cs)<120: return
    closes = [float(c[4]) for c in cs]
    vols   = [float(c[5]) for c in cs]
    # Δ15, Δ60 للسلاسل التاريخية عبر انزلاق
    def series_delta(win):
        out=[]
        for i in range(win, len(closes)):
            a=closes[i]; b=closes[i-win]
            out.append(pct(a,b))
        return out
    del15 = series_delta(15)
    del60 = series_delta(60)
    def mean_std(x):
        if not x: return (0.0, 1.0)
        m = sum(x)/len(x)
        var = sum((xi-m)**2 for xi in x)/max(1,len(x))
        sd = math.sqrt(var) if var>1e-12 else 1.0
        return (m, sd)
    mu15, sd15 = mean_std(del15)
    mu60, sd60 = mean_std(del60)
    muvol, sdvol = mean_std(vols)
    p = {
        "mu15": mu15, "sd15": sd15,
        "mu60": mu60, "sd60": sd60,
        "muvol": muvol, "sdvol": sdvol
    }
    r.hset(KEY_PROFILE(sym_market), mapping={k: str(v) for k,v in p.items()})
    r.expire(KEY_PROFILE(sym_market), 3*3600)

def profile_builder_loop():
    # يبني/يحدّث بروفايلات لكل الأسواق كل ساعة
    while True:
        try:
            mkts = get_markets_eur()
            for m in mkts:
                try:
                    build_profile(m)
                except Exception as e:
                    print("profile err", m, e)
                time.sleep(0.03)
        except Exception as e:
            print("profile loop err:", e)
        time.sleep(PROFILE_PERIOD_SEC)

# =========================
# 🎯 درجة فورية + تنعيم (EMA)
# =========================
def last_two_swings_low(closes, depth):
    lows=[]
    for i in range(depth, len(closes)-depth):
        left  = all(closes[i] <= closes[k] for k in range(i-depth, i))
        right = all(closes[i] <= closes[k] for k in range(i+1, i+1+depth))
        if left and right: lows.append((i, closes[i]))
    return lows[-2:] if len(lows)>=2 else None

def compute_instant_score(sym_market):
    # نحتاج ~120 دقيقة للمقاييس الآنية
    cs = get_candles_1m(sym_market, limit=130)
    if len(cs)<70: return None
    closes=[float(c[4]) for c in cs]
    vols  =[float(c[5]) for c in cs]
    p_now = closes[-1]
    if p_now < MIN_PRICE_EUR: return None

    # deltas
    d15 = pct(closes[-1], closes[-16]) if len(closes)>=16 else 0.0
    d60 = pct(closes[-1], closes[-61]) if len(closes)>=62 else pct(closes[-1], closes[0])
    accel = d15 - d60

    # vol z
    prof = {k.decode(): float(v.decode()) for k,v in r.hgetall(KEY_PROFILE(sym_market)).items()}
    muvol = prof.get("muvol", 0.0); sdvol = max(1e-9, prof.get("sdvol", 1.0))
    z_vol = (vols[-1]-muvol)/sdvol
    z_vol = max(-3.0, min(3.0, z_vol))

    # z for 15,60 vs profile
    mu15 = prof.get("mu15", 0.0); sd15=max(1e-9,prof.get("sd15",1.0))
    mu60 = prof.get("mu60", 0.0); sd60=max(1e-9,prof.get("sd60",1.0))
    z15  = (d15 - mu15)/sd15
    z60  = (d60 - mu60)/sd60

    # drawdown من قمة آخر ساعتين
    hi2h = max(closes[-120:]) if len(closes)>=120 else max(closes)
    drawdown = pct(closes[-1], hi2h)  # سالب عند الهبوط

    # Fresh bonus: إذا عادة هادئة والآن حركتها 15m قوية مع حجم
    fresh = 0.5 if (sd15<0.6 and d15>1.0 and z_vol>1.5) else 0.0

    # Instant score
    instant = (0.8*z15 + 0.6*z60 + 0.7*accel/1.0 + 0.4*z_vol - 0.5*max(0, -drawdown) + fresh)

    # تخزين high2h الآنية
    st = r.hgetall(KEY_STATE(sym_market))
    prev_high2h = float(st.get(b"high2h", b"0") or 0)
    if hi2h > prev_high2h:
        r.hset(KEY_STATE(sym_market), "high2h", hi2h)
    r.hset(KEY_STATE(sym_market), "last_price", p_now)

    # فحص خروج طارئ: 10m وكسر L2
    ch10 = pct(closes[-1], closes[-11]) if len(closes)>=12 else 0.0
    swings = last_two_swings_low(closes[-70:], SWING_DEPTH) if len(closes)>=70 else None
    l2_broken = False
    if swings:
        (_, L1), (idx2, L2) = swings
        if any(p < L2 for p in closes[idx2:]): l2_broken = True

    return {
        "instant": instant,
        "d15": d15, "d60": d60, "accel": accel,
        "z15": z15, "z60": z60, "zvol": z_vol,
        "drawdown": drawdown,
        "ch10": ch10,
        "emergency": (ch10 <= EMERGENCY_CH10 and l2_broken)
    }

def ema_update(sym_market):
    st = {k.decode(): float(v.decode()) for k,v in r.hgetall(KEY_STATE(sym_market)).items()}
    base = compute_instant_score(sym_market)
    if not base: return None
    smooth_prev = st.get("score_smooth", 0.0)
    smooth = ALPHA_EMA*base["instant"] + (1-ALPHA_EMA)*smooth_prev

    # streaks
    up_streak = int(st.get("up_streak", 0))
    down_streak = int(st.get("down_streak", 0))
    if smooth >= ENTRY_TH:
        up_streak += 1
    else:
        up_streak = 0
    if smooth <= EXIT_TH:
        down_streak += 1
    else:
        down_streak = 0

    r.hset(KEY_STATE(sym_market), mapping={
        "score_smooth": smooth,
        "up_streak": up_streak,
        "down_streak": down_streak
    })

    return {"smooth": smooth, "up": up_streak, "down": down_streak, **base}

def markets_loop_update():
    # تحديث درجات كل الأسواق دوريًا
    while True:
        try:
            mkts = get_markets_eur()
            for m in mkts:
                try:
                    ema_update(m)
                except Exception as e:
                    print("ema err", m, e)
                time.sleep(0.02)
        except Exception as e:
            print("markets loop err:", e)
        time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 🏆 إدارة الغرفة (امتلاك التوب 10 بسلاسة)
# =========================
def in_room(market): 
    return r.sismember(KEY_ROOM, market)

def room_members():
    return [b.decode() for b in r.smembers(KEY_ROOM)]

def room_scores():
    rows=[]
    for m in room_members():
        s = float((r.hget(KEY_STATE(m), "score_smooth") or b"0").decode() or 0)
        rows.append((m, s))
    rows.sort(key=lambda x:x[1], reverse=True)
    return rows

def swap_limit_reached():
    if r.exists(KEY_SWAP_COOLDOWN): 
        return True
    cnt = int(r.get(KEY_SWAP_BUCKET) or 0)
    return cnt >= SWAP_SOFT_LIMIT

def register_swap():
    r.incr(KEY_SWAP_BUCKET)
    r.expire(KEY_SWAP_BUCKET, SWAP_WINDOW_SEC)
    r.setex(KEY_SWAP_COOLDOWN, GLOBAL_SWAP_COOLDOWN_S, 1)

def maybe_rebuild_room():
    # يُستدعى كل REBUILD_PERIOD_SEC
    mkts = get_markets_eur()
    # مرشحين: أعلى smooth خارج الغرفة
    candidates=[]
    for m in mkts:
        s = float((r.hget(KEY_STATE(m),"score_smooth") or b"0").decode() or 0)
        up = int((r.hget(KEY_STATE(m),"up_streak") or b"0").decode() or 0)
        if not in_room(m) and up >= CONFIRM_UP_PASSES and s > 0:
            candidates.append((m, s))
    candidates.sort(key=lambda x:x[1], reverse=True)

    # إبقِ الموجودين إذا ما صار ضعف مؤكد
    current = room_scores()
    # خروج طارئ أولًا
    to_remove=[]
    for m, s in current:
        down = int((r.hget(KEY_STATE(m),"down_streak") or b"0").decode() or 0)
        emerg = json.loads((r.hget(KEY_STATE(m),"flags") or b"{}").decode() or "{}").get("emergency", False)
        # احصل على آخر قيم آنية لتحديد الطارئ
        base = compute_instant_score(m)
        emerg = base["emergency"] if base else False
        if (down >= CONFIRM_DOWN_PASSES) or emerg:
            to_remove.append(m)

    # نفّذ الإخراج الطارئ/المؤكد
    removed=0
    for m in to_remove:
        r.srem(KEY_ROOM, m)
        removed += 1

    # أكمل الغرفة إلى TOP_N
    current = room_scores()
    need = max(0, TOP_N - len(current))
    added=0
    for m, s in candidates:
        if in_room(m): 
            continue
        if swap_limit_reached() and need==0:
            break
        r.sadd(KEY_ROOM, m)
        added += 1
        register_swap()
        if len(room_members()) >= TOP_N:
            break

    # استبدال Gap: لو غرفة ممتلئة، بدّل الأضعف إذا مرشح قوي يتجاوزه بـ GAP
    if not swap_limit_reached():
        current = room_scores()
        if current:
            weakest_m, weakest_s = current[-1]
            for m, s in candidates:
                if in_room(m): 
                    continue
                if s >= weakest_s + REPLACEMENT_GAP:
                    r.srem(KEY_ROOM, weakest_m)
                    r.sadd(KEY_ROOM, m)
                    register_swap()
                    break

def rebuild_loop():
    while True:
        try:
            # وسم آخر إعادة (للتشخيص)
            r.set(KEY_LAST_REBUILD, int(time.time()))
            maybe_rebuild_room()
        except Exception as e:
            print("rebuild err:", e)
        time.sleep(REBUILD_PERIOD_SEC)

# =========================
# 📊 فلتر السجل: ترند نظيف (HL + دون كسر + صعود 1h)
# =========================
def is_strong_clean_uptrend(market):
    cs = get_candles_1m(market, limit=90)
    if len(cs)<30: return False
    closes=[float(c[4]) for c in cs]
    p_now = closes[-1]
    base  = closes[-61] if len(closes)>=62 else closes[0]
    ch1h  = pct(p_now, base)
    if ch1h < UP_CH1H_MIN: return False
    # لا شمعة <= -2% خلال آخر ساعة
    start_idx=max(1, len(closes)-61)
    for i in range(start_idx, len(closes)):
        step = pct(closes[i], closes[i-1])
        if step <= MAX_RED_CANDLE:
            return False
    swings = last_two_swings_low(closes, SWING_DEPTH)
    if not swings: return False
    (_, L1), (i2, L2) = swings
    if pct(L2, L1) < HL_MIN_GAP_PCT: return False
    if any(p < L2 for p in closes[i2:]): return False
    if pct(p_now, L2) < MIN_ABOVE_L2_PCT: return False
    return True

# =========================
# 🧹 تهيئة / مسح Redis
# =========================
def reset_all():
    if CLEAR_ON_START:
        for k in r.scan_iter(f"{NS}:*"):
            r.delete(k)
        print("🧹 Redis cleared")
    r.delete(KEY_SWAP_BUCKET)
    r.delete(KEY_SWAP_COOLDOWN)
    r.delete(KEY_ROOM)

# =========================
# 🔌 Telegram Webhook
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
    text = text.replace("\u200f","").replace("\u200e","").strip()
    if not text: return ""
    first = text.split()[0]
    if "@" in first: first = first.split("@",1)[0]
    return first.lower()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    raw, cid = _extract_update_fields(data)
    cmd = _normalize_cmd(raw)
    if cmd in ("/start","ابدأ","start"):
        send_msg("✅ الصيّاد الذكي يعمل.\nأرسل /السجل لعرض التوب 10 المستقر.\nأرسل /diag للتشخيص.", cid)
    elif cmd in ("/السجل","السجل","/log","log","/snapshot","snapshot"):
        rows = room_scores()
        # فلتر النظافة
        final=[]
        for m,s in rows:
            if is_strong_clean_uptrend(m):
                p = float((r.hget(KEY_STATE(m),"last_price") or b"0").decode() or 0)
                # بعض القياسات للعرض
                d15 = compute_instant_score(m)
                if d15:
                    final.append((m,s,p,d15["d15"],d15["d60"],d15["zvol"],d15["drawdown"]))
                if len(final)>=TOP_N: break
        if not final:
            send_msg("⚠️ لا نتائج مطابقة لفلتر الترند النظيف حالياً.", cid)
        else:
            lines=[]
            for i,(m,s,p,d15,d60,zv,dd) in enumerate(final,1):
                coin=m.split("-")[0]
                lines.append(f"{i:02d}. ✅ {coin} | score {s:.2f} | Δ15 {d15:.2f}% | Δ60 {d60:.2f}% | volZ {zv:.1f} | DD {dd:.2f}% | {p:.6f}€")
            send_msg("📊 Top10 (EMA + تأكيد) — ترند نظيف:\n" + "\n".join(lines), cid)
    elif cmd in ("/diag","تشخيص","/تشخيص"):
        rm = room_members()
        last_rb = int(r.get(KEY_LAST_REBUILD) or 0)
        ago = int(time.time()) - last_rb if last_rb else -1
        send_msg(f"🔎 room={len(rm)} | swap_used={int(r.get(KEY_SWAP_BUCKET) or 0)}/{SWAP_SOFT_LIMIT} | cooldown={'on' if r.exists(KEY_SWAP_COOLDOWN) else 'off'} | last_rebuild_ago={ago}s", cid)
    return "ok", 200

# =========================
# ▶️ التشغيل
# =========================
if __name__ == "__main__":
    reset_all()
    send_msg("🚀 بدأ الصيّاد الذكي: امتلاك Top10 بالـ EMA + تأكيد + حد تبديل لطيف.")

    Thread(target=profile_builder_loop, daemon=True).start()
    Thread(target=markets_loop_update, daemon=True).start()
    Thread(target=rebuild_loop, daemon=True).start()

    port=int(os.getenv("PORT",8080))
    app.run(host="0.0.0.0", port=port)