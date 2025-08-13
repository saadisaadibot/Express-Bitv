# -*- coding: utf-8 -*-
"""
Bot B — الحاضنة اليقِظة (Sniper / Decision Engine)
- يستقبل CV من Bot A عبر /ingest
- يراقب السعر فقط (ticker/price) على دفعات خفيفة
- يطبق قواعد (Gradual+Nudge / Fast Burst / Accum) ويرسل إشعار شراء (Webhook/Telegram)
- بدون Redis. كل شيء بالذاكرة.
- قصّ ذكي عند امتلاء الغرفة. TTL + Sticky للفُرص الجديدة.
"""

import os, time, math, json, random, threading
from collections import deque, defaultdict
import requests
from flask import Flask, request, jsonify

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
# Bitvavo
BITVAVO_URL         = os.getenv("BITVAVO_URL", "https://api.bitvavo.com")
HTTP_TIMEOUT        = float(os.getenv("HTTP_TIMEOUT", 8.0))

# غرفة ومراقبة
ROOM_CAP            = int(os.getenv("ROOM_CAP", 24))          # أقصى عدد رموز تحت المراقبة
TTL_MIN             = int(os.getenv("TTL_MIN", 30))           # مدة بقاء الرمز (دقائق)
STICKY_MIN          = int(os.getenv("STICKY_MIN", 5))         # فترة سماح بعد الدخول (دقائق)
TICK_SEC            = float(os.getenv("TICK_SEC", 2.5))       # دورة المراقبة (ثوانٍ)
BATCH_SIZE          = int(os.getenv("BATCH_SIZE", 12))        # عدد الأسواق بكل دفعة

# تبريد وإشعارات
ALERT_COOLDOWN_SEC  = int(os.getenv("ALERT_COOLDOWN_SEC", 120))
SPREAD_MAX_BP       = int(os.getenv("SPREAD_MAX_BP", 30))     # 0.30% كحد أقصى
COIN_SILENT_SEC     = int(os.getenv("COIN_SILENT_SEC", 5))    # صمت بعد دخول CV

# عتبات القرار (قريبة من إعداداتك المتوازنة)
TRIG_R40            = float(os.getenv("TRIG_R40", 0.40))      # %
TRIG_R120           = float(os.getenv("TRIG_R120", 0.80))     # %
TRIG_VOLZ           = float(os.getenv("TRIG_VOLZ", 1.00))     # من CV (A)

# قواعد الترند التدريجي (من A) + تكة (من السعر الحيّ)
GRAD_R600           = float(os.getenv("GRAD_R600", 3.00))     # %
GRAD_DD300_MAX      = float(os.getenv("GRAD_DD300_MAX", 1.00))# %
NUDGE_R60           = float(os.getenv("NUDGE_R60", 0.15))     # %
NUDGE_R40           = float(os.getenv("NUDGE_R40", 0.20))     # %

# Telegram / Webhook (اختياري)
BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
CHAT_ID             = os.getenv("CHAT_ID", "")
SAQR_WEBHOOK        = os.getenv("SAQR_WEBHOOK", "")           # مثلاً أمر شراء

# تكيّف خفيف لكل عملة (bandit-like)
TUNE_WIN_SEC        = int(os.getenv("TUNE_WIN_SEC", 120))     # نقيم الإشارة بعد 120s
TUNE_STEP           = float(os.getenv("TUNE_STEP", 0.05))     # تعديل محلي للحدود ±
TUNE_MAX_ABS        = float(os.getenv("TUNE_MAX_ABS", 0.20))  # حد أقصى للتعديل التراكمي

# =========================
# 🌐 HTTP Session
# =========================
session = requests.Session()
session.headers.update({"User-Agent":"Warden-Sniper/1.0"})
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter); session.mount("http://", adapter)

def http_get(path, params=None, base=BITVAVO_URL, timeout=HTTP_TIMEOUT):
    url = f"{base}{path}"
    try:
        r = session.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            time.sleep(0.6 + random.random()*0.6)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[HTTP] GET {path} failed: {e}")
        return None

# =========================
# 🧰 أدوات
# =========================
def pct(a, b):
    if b is None or b == 0: return 0.0
    return (a - b) / b * 100.0

def now(): return time.time()

# =========================
# 🔔 إشعارات
# =========================
def tg_send(text):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=8)
    except Exception as e:
        print("[TG] send failed:", e)

def notify_buy(market, reason):
    msg = f"🚀 BUY {market}  | {reason}"
    print("[ALERT]", msg)
    tg_send(msg)
    if SAQR_WEBHOOK:
        try:
            session.post(SAQR_WEBHOOK, json={"text": f"اشتري {market}", "reason": reason}, timeout=6)
        except Exception as e:
            print("[SAQR] post failed:", e)

# =========================
# 🧠 حالة الرمز
# =========================
class Coin:
    __slots__ = (
        "market","symbol",
        "entered_at","expires_at","sticky_until",
        "last_alert_at","last_cv_at","silent_until",
        "cv","tags",
        "buf","last_price",
        "tune_bias","outcomes","pending_eval"
    )
    def __init__(self, market, symbol):
        self.market = market
        self.symbol = symbol
        t = now()
        self.entered_at = t
        self.expires_at = t + TTL_MIN*60
        self.sticky_until = t + STICKY_MIN*60
        self.last_alert_at = 0
        self.last_cv_at = 0
        self.silent_until = t + COIN_SILENT_SEC
        self.cv = {}         # آخر CV من A
        self.tags = []
        self.buf = deque(maxlen=int(max(600, 900/TICK_SEC)))  # ~10-15 دقيقة
        self.last_price = None
        self.tune_bias = 0.0  # ± تعديل محلي على حدود r40/r120
        self.outcomes = deque(maxlen=8)  # سجل نجاح/فشل
        self.pending_eval = [] # [(t_alert, price_at_alert)]

    def renew(self, ttl_sec):
        self.expires_at = max(self.expires_at, now() + ttl_sec)

    # تغيّر خلال آخر N ثوانٍ من buffer
    def r_change(self, seconds):
        if len(self.buf) < 2: return 0.0
        t_now, p_now = self.buf[-1]
        t_target = t_now - seconds
        base = None
        for (t,p) in reversed(self.buf):
            if t <= t_target:
                base = p; break
        if base is None: base = self.buf[0][1]
        return pct(p_now, base)

# =========================
# 🗃️ غرفة وإدارة
# =========================
room_lock = threading.Lock()
room = {}   # market -> Coin

def ensure_coin(cv):
    m = cv["market"]; sym = cv.get("symbol", m.split("-")[0])
    with room_lock:
        c = room.get(m)
        if not c:
            c = Coin(m, sym)
            room[m] = c
        c.cv = cv["feat"]
        c.tags = cv.get("tags", [])
        c.last_cv_at = now()
        c.silent_until = now() + COIN_SILENT_SEC
        c.renew(cv.get("ttl_sec", TTL_MIN*60))
        # قص زائد لو لزم
        overflow = len(room) - ROOM_CAP
        if overflow > 0:
            # قص ذكي: أبقِ الجديد والفعّال
            scored = []
            tnow = now()
            for mk, st in room.items():
                r60  = st.r_change(60)
                r120 = st.r_change(120)
                vz   = float(st.cv.get("volZ", 0.0)) if st.cv else 0.0
                age_min = (tnow - st.entered_at)/60.0
                score = 0.7*r60 + 0.3*r120 + 0.5*vz
                if tnow < st.sticky_until: score += 5.0
                score -= 0.02*age_min
                scored.append((score, mk))
            scored.sort()
            for _, mk in scored[:overflow]:
                room.pop(mk, None)

# =========================
# 📈 جلب السعر الحيّ
# =========================
def fetch_price(market):
    data = http_get("/v2/ticker/price", params={"market": market})
    if not data: return None
    try:
        return float(data.get("price") or 0.0)
    except: return None

# =========================
# 🧠 قواعد القرار
# =========================
def decide(market, c: Coin, ts, price):
    # حماية عامة
    if ts < c.silent_until: return None
    if (ts - c.last_alert_at) < ALERT_COOLDOWN_SEC: return None

    # مؤشرات لحظية من الـbuffer
    r20  = c.r_change(20)
    r40  = c.r_change(40)
    r60  = c.r_change(60)
    r120 = c.r_change(120)

    # ميزات من CV (A)
    cv = c.cv or {}
    r300  = float(cv.get("r300", 0.0))
    r600  = float(cv.get("r600", 0.0))
    dd300 = float(cv.get("dd300", 9e9))
    volZ  = float(cv.get("volZ", 0.0))
    spread_bp = float(cv.get("spread_bp", 999))
    pct24 = float(cv.get("pct24", 0.0))
    liq_rank = int(cv.get("eur_liq_rank", 9999))

    # سبريد حماية
    if spread_bp > SPREAD_MAX_BP:
        return None

    # تكيّف محلي بسيط
    bias = max(-TUNE_MAX_ABS, min(TUNE_MAX_ABS, c.tune_bias))
    R40 = TRIG_R40 + bias
    R120= TRIG_R120 + bias

    reasons = []

    # 1) Gradual + Nudge
    if (r600 >= GRAD_R600 and dd300 <= GRAD_DD300_MAX):
        if (r60 >= NUDGE_R60) or (r40 >= NUDGE_R40):
            if (volZ >= 0.9) or (liq_rank <= 60):
                reasons.append(f"Gradual+Nudge r600={r600:.2f}% dd300={dd300:.2f}% r60={r60:.2f}% volZ={volZ:.2f}")

    # 2) Fast Burst
    if (r40 >= R40 and r120 >= R120) and ((volZ >= TRIG_VOLZ) or (r40 >= R40 + 0.10)):
        reasons.append(f"FastBurst r40={r40:.2f}% r120={r120:.2f}% volZ={volZ:.2f}")

    # 3) Accumulation
    if (r120 >= R120 and r20 >= 0.20 and volZ >= 1.0):
        reasons.append(f"Accum r120={r120:.2f}% r20={r20:.2f}% volZ={volZ:.2f}")

    if not reasons:
        return None

    reason = reasons[0]  # أول مايُحقق شرط
    # سجّل تنبيه + جدولة تقييم النتيجة بعد TUNE_WIN_SEC
    c.last_alert_at = ts
    c.pending_eval.append((ts, price))
    return reason

# =========================
# 🧪 تقييم الإشارات (تكيّف محلي)
# =========================
def eval_loop():
    while True:
        try:
            nowt = now()
            with room_lock:
                items = list(room.items())
            for m, c in items:
                # قيّم كل إشعار مرّ عليه TUNE_WIN_SEC
                keep = []
                for (t_alert, p0) in c.pending_eval:
                    if nowt - t_alert < TUNE_WIN_SEC:
                        keep.append((t_alert, p0)); continue
                    # احصل على آخر سعر
                    p_now = c.last_price if c.last_price else p0
                    ret = pct(p_now, p0)
                    ok = (ret >= 0.40)  # نجح إذا +0.4% خلال 120s
                    c.outcomes.append(1 if ok else 0)
                    # عدّل bias
                    if ok and c.tune_bias > -TUNE_MAX_ABS:
                        c.tune_bias = max(-TUNE_MAX_ABS, c.tune_bias - TUNE_STEP)
                    elif (not ok) and c.tune_bias < TUNE_MAX_ABS:
                        c.tune_bias = min(TUNE_MAX_ABS, c.tune_bias + TUNE_STEP)
                c.pending_eval = keep
        except Exception as e:
            print("[EVAL] error:", e)
        time.sleep(5)

# =========================
# 🩺 حلقة المراقبة
# =========================
backoff = False
def monitor_loop():
    global backoff
    rr = 0
    while True:
        t0 = now()
        try:
            with room_lock:
                markets = list(room.keys())
            if not markets:
                time.sleep(TICK_SEC); continue

            batch = markets[rr:rr+BATCH_SIZE]
            if not batch:
                rr = 0
                batch = markets[:BATCH_SIZE]
            rr += BATCH_SIZE

            errors = 0
            for m in batch:
                p = fetch_price(m)
                if p is None:
                    errors += 1; continue
                ts = now()
                with room_lock:
                    c = room.get(m)
                    if not c: continue
                    c.last_price = p
                    c.buf.append((ts, p))
                    # انتهاء TTL
                    if ts >= c.expires_at:
                        room.pop(m, None)
                        continue
                    # قرار
                    reason = decide(m, c, ts, p)
                    if reason:
                        notify_buy(m, reason)

            backoff = (errors >= max(3, len(batch)//3))
        except Exception as e:
            print("[MONITOR] error:", e)
            backoff = True

        base = TICK_SEC if not backoff else max(TICK_SEC, 5.0)
        time.sleep(max(0.2, base + random.uniform(0.05, 0.25) - (now() - t0)))

# =========================
# 🌐 Flask API
# =========================
app = Flask(__name__)

@app.route("/")
def root():
    return "Warden Sniper B is alive ✅"

@app.route("/ingest", methods=["POST"])
def ingest():
    """
    يستقبل CV من Bot A:
    {
      "market":"XYZ-EUR",
      "symbol":"XYZ",
      "ts": 1723...,
      "feat": { r300,r600,r1800,dd300,volZ,spread_bp,pct24,eur_liq_rank },
      "tags":[...],
      "ttl_sec": 1800
    }
    """
    try:
        cv = request.get_json(force=True, silent=True) or {}
        market = cv.get("market","")
        feat = cv.get("feat",{})
        if not market or not feat:
            return jsonify(ok=False, err="bad payload"), 400
        ensure_coin(cv)
        return jsonify(ok=True)
    except Exception as e:
        print("[INGEST] err:", e)
        return jsonify(ok=False), 200

@app.route("/status")
def status():
    with room_lock:
        n = len(room)
        rows = []
        for m, c in room.items():
            r60 = c.r_change(60)
            r120= c.r_change(120)
            vz  = float((c.cv or {}).get("volZ", 0.0))
            ttl = max(0, int(c.expires_at - now()))
            rows.append(f"• {m:<12} r60={r60:+.2f}% r120={r120:+.2f}% VolZ={vz:+.2f} TTL={ttl}s bias={c.tune_bias:+.2f}")
    hdr  = f"📊 Status — Room: {n}/{ROOM_CAP} | Backoff: {'ON' if backoff else 'OFF'}"
    rules= (f"\n🔍 Gradual+Nudge: r600≥{GRAD_R600:.2f}%, dd300≤{GRAD_DD300_MAX:.2f}% + "
            f"(r60≥{NUDGE_R60:.2f}% or r40≥{NUDGE_R40:.2f}%) & VolZ≥0.9 or Rank≤60"
            f"\n⚡ FastBurst: r40≥{TRIG_R40:.2f}% & r120≥{TRIG_R120:.2f}% & (VolZ≥{TRIG_VOLZ:.2f} or r40≥{TRIG_R40+0.10:.2f}%)"
            f"\n📈 Accum: r120≥{TRIG_R120:.2f}% & r20≥0.20% & VolZ≥1.0"
            f"\n🛡️ Spread≤{SPREAD_MAX_BP/100:.2f}%, Cooldown={ALERT_COOLDOWN_SEC}s, Sticky={STICKY_MIN}m"
            f"\n🎯 Tune: step={TUNE_STEP:+.2f}, window={TUNE_WIN_SEC}s, max|bias|={TUNE_MAX_ABS:.2f}")
    return (hdr + "\n" + rules + "\n\n" + "\n".join(rows)), 200, {"Content-Type":"text/plain; charset=utf-8"}

# =========================
# ▶️ التشغيل
# =========================
def start_threads():
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=eval_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    app.run(host="0.0.0.0", port=port)