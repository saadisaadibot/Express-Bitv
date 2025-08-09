# -*- coding: utf-8 -*-
import os, time, json, math, requests, redis
from flask import Flask, request
from collections import deque, defaultdict
from threading import Thread, Lock
from dotenv import load_dotenv

load_dotenv()

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 10))     # كل كم ثانية نفحص
TOP_N_PREWATCH       = int(os.getenv("TOP_N_PREWATCH", 25))    # حجم مجموعة المراقبة المبدئية (5m)
VOL_SPIKE_MULT       = float(os.getenv("VOL_SPIKE_MULT", 1.4)) # مضاعفة حجم الدقيقة
MIN_DAILY_EUR        = float(os.getenv("MIN_DAILY_EUR", 10000))# حد أدنى لقيمة تداول يومية €
BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 360)) # منع تكرار الإشعار لنفس العملة
RANK_JUMP_STEPS      = int(os.getenv("RANK_JUMP_STEPS", 15))   # قفزة ترتيب مطلوبة
RANK_REFRESH_SEC     = int(os.getenv("RANK_REFRESH_SEC", 30))  # كل كم ثانية نعيد حساب ترتيب السوق
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))  # نافذة نمط السلّم
STEP_PCT             = float(os.getenv("STEP_PCT", 0.8))       # حجم كل خطوة في السلّم %
STEP_BACKDRIFT       = float(os.getenv("STEP_BACKDRIFT", 0.3)) # السماح بهبوط عكسي بين الخطوتين %
BREAKOUT_LOOKBACK_M  = int(os.getenv("BREAKOUT_LOOKBACK_M", 10))# قمة محلية: آخر N دقائق
BREAKOUT_BUFFER_PCT  = float(os.getenv("BREAKOUT_BUFFER_PCT", 0.7)) # تجاوز القمة %

# =========================
# 🧹 إعدادات تنظيف الذاكرة
# =========================
CLEAN_ON_BOOT        = int(os.getenv("CLEAN_ON_BOOT", "1"))     # 1 = امسح عند الإقلاع
STATE_TTL_SEC        = int(os.getenv("STATE_TTL_SEC", "3600"))  # حد صلاحية الحالات المؤقتة
GC_INTERVAL_SEC      = int(os.getenv("GC_INTERVAL_SEC", "300")) # كل كم ثانية نجري GC
REDIS_NS             = os.getenv("REDIS_NS", "trend")           # بادئة مفاتيح ريديس لهاد البوت فقط

# =========================
# 🔐 تكاملات
# =========================
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHAT_ID       = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")  # مثال: https://your-app/endpoint
REDIS_URL     = os.getenv("REDIS_URL")

app = Flask(__name__)
r = redis.from_url(REDIS_URL) if REDIS_URL else None
lock = Lock()

# =========================
# 🧠 حالات داخلية
# =========================
supported    = set()                      # رموز EUR المدعومة
step_state   = {}                         # sym -> {"base":price, "p1":(price,t), "t0":ts}
last_alert   = {}                         # sym -> ts
vol_cache    = defaultdict(lambda: deque(maxlen=20))  # (ts, close, vol_1m)
rank_map     = {}                         # market -> rank على 5m
info_map     = {}                         # market -> (ch5, price, vspike)
last_rank    = {}                         # market -> (rank, ts)
high10       = defaultdict(lambda: deque(maxlen=BREAKOUT_LOOKBACK_M))  # لكل عملة: [(ts, close)]
last_touch   = {}                         # لمتابعة صلاحيات GC
last_rank_refresh = 0.0                   # آخر تحديث لترتيب السوق

# =========================
# 📮 مراسلة
# =========================
def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}, timeout=8
        )
    except Exception as e:
        print("TG error:", e)

def send_to_saqr(symbol):
    if not SAQAR_WEBHOOK:
        return
    try:
        payload = {"message": {"text": f"اشتري {symbol}"}}
        resp = requests.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        print("Send Saqr:", resp.status_code, resp.text[:180])
    except Exception as e:
        print("Saqr error:", e)

# =========================
# 🔧 Bitvavo helpers
# =========================
def get_markets_eur():
    global supported
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets", timeout=10).json()
        supported = {m["market"].replace("-EUR","") for m in res if m.get("market","").endswith("-EUR")}
        return [m["market"] for m in res if m.get("market","").endswith("-EUR")]
    except Exception as e:
        print("markets error:", e)
        return []

def get_candles(market, interval="1m", limit=20):
    try:
        return requests.get(
            f"https://api.bitvavo.com/v2/{market}/candles?interval={interval}&limit={limit}",
            timeout=10
        ).json()
    except Exception as e:
        print("candles error:", market, e)
        return []

def get_24h_stats():
    try:
        arr = requests.get("https://api.bitvavo.com/v2/ticker/24h", timeout=10).json()
        out = {}
        for row in arr:
            market = row.get("market")
            if not market or not market.endswith("-EUR"): 
                continue
            vol_eur = float(row.get("volume", 0)) * float(row.get("last", 0))
            out[market] = vol_eur
        return out
    except Exception:
        return {}

def compute_5m_change(market):
    c = get_candles(market, "1m", 6)  # 6 شموع احتياط
    if not isinstance(c, list) or len(c) < 5:
        return None, None, None
    closes = [float(x[4]) for x in c]
    p_now = closes[-1]; p_5m = closes[0]
    change = (p_now - p_5m) / p_5m * 100 if p_5m > 0 else 0.0

    c15 = get_candles(market, "1m", 16)
    if not isinstance(c15, list) or len(c15) < 3:
        v_spike = 1.0
    else:
        vol_last = float(c15[-1][5])
        avg_prev = sum(float(x[5]) for x in c15[:-1]) / max(len(c15)-1, 1)
        v_spike = (vol_last/avg_prev) if avg_prev > 0 else 1.0

    return change, p_now, v_spike

def compute_15m_change(market):
    c = get_candles(market, "1m", 16)
    if not isinstance(c, list) or len(c) < 15:
        return None
    close_now = float(c[-1][4]); close_15 = float(c[0][4])
    return (close_now - close_15) / close_15 * 100 if close_15 > 0 else 0.0

# =========================
# 🧮 ترتيب السوق (5m)
# =========================
def rank_all_5m(markets):
    rows = []
    for m in markets:
        ch, p, vsp = compute_5m_change(m)
        if ch is None:
            continue
        rows.append((m, ch, p, vsp))
    rows.sort(key=lambda x: x[1], reverse=True)
    rmap = {m:i+1 for i,(m,_,_,_) in enumerate(rows)}
    imap = {m:(ch,p,vsp) for (m,ch,p,vsp) in rows}
    return rmap, imap

# =========================
# 🧼 إدارة الحالة والتنظيف
# =========================
def touch(key):  # حدّث آخر استعمال لمفتاح حالة
    last_touch[key] = time.time()

def reset_state():
    step_state.clear()
    last_alert.clear()
    vol_cache.clear()
    high10.clear()
    last_rank.clear()
    rank_map.clear()
    info_map.clear()
    last_touch.clear()

def clear_redis_namespace():
    if not r:
        return
    patterns = [f"{REDIS_NS}:*", "alerted:*"]
    for pat in patterns:
        try:
            for k in r.scan_iter(pat):
                r.delete(k)
        except Exception as e:
            print("redis clear error:", e)

def gc_loop():
    while True:
        try:
            now = time.time()
            # نظف step_state
            for sym in list(step_state.keys()):
                k = f"step:{sym}"
                if now - last_touch.get(k, 0) > STATE_TTL_SEC:
                    step_state.pop(sym, None)
            # نظف high10
            for m in list(high10.keys()):
                k = f"hi10:{m}"
                if now - last_touch.get(k, 0) > STATE_TTL_SEC:
                    high10.pop(m, None)
            # نظف last_rank
            for m in list(last_rank.keys()):
                _, tsr = last_rank[m]
                if now - tsr > STATE_TTL_SEC:
                    last_rank.pop(m, None)
        except Exception as e:
            print("gc error:", e)
        time.sleep(GC_INTERVAL_SEC)

# =========================
# 🧩 منطق الإشعار
# =========================
def should_alert(sym):
    ts = time.time()
    last = last_alert.get(sym, 0)
    # تحقق من redis أيضاً إذا موجود
    if r:
        if r.get(f"alerted:{sym}"):
            return False
    return (ts - last) >= BUY_COOLDOWN_SEC

def mark_alerted(sym):
    ts = time.time()
    last_alert[sym] = ts
    if r:
        r.setex(f"alerted:{sym}", BUY_COOLDOWN_SEC, 1)

def in_step_pattern(sym, price_now, ts):
    """سلّم مرن: +STEP% ثم +STEP% ثانية ضمن نافذة STEP_WINDOW_SEC مع سماح هبوط عكسي."""
    touch(f"step:{sym}")
    st = step_state.get(sym)
    if st is None:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False, "init"

    if ts - st["t0"] > STEP_WINDOW_SEC:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False, "window_reset"

    base = st["base"]
    if base <= 0:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False, "bad_base"

    step_pct_now = (price_now - base) / base * 100

    # هبوط عكسي كبير يلغي المسار
    if step_pct_now < -STEP_BACKDRIFT:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False, "dip_reset"

    if not st["p1"]:
        if step_pct_now >= STEP_PCT:
            st["p1"] = (price_now, ts)
            return False, "step1_ok"
        return False, "progress"
    else:
        p1_price, _ = st["p1"]
        if (price_now - p1_price) / p1_price * 100 >= STEP_PCT:
            step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
            return True, "step2_ok"
        return False, "waiting_step2"

def is_rank_jump(market, rank_now, ts):
    prev = last_rank.get(market)
    last_rank[market] = (rank_now, ts)
    if not prev:
        return False
    r_old, t_old = prev
    return (r_old - rank_now) >= RANK_JUMP_STEPS and (ts - t_old) <= 180

def update_high10(market, close, ts):
    touch(f"hi10:{market}")
    dq = high10[market]
    # نخزن كل دقيقة تقريباً
    if not dq or ts - dq[-1][0] >= 60:
        dq.append((ts, close))

def is_breakout_local(market, close_now):
    dq = high10[market]
    if len(dq) < max(3, BREAKOUT_LOOKBACK_M//2):
        return False
    high_local = max(x[1] for x in dq)
    return (close_now - high_local) / high_local * 100 >= BREAKOUT_BUFFER_PCT

# =========================
# 🔁 حلقة المراقبة
# =========================
def monitor_loop():
    global rank_map, info_map, last_rank_refresh
    markets = get_markets_eur()
    if not markets:
        print("No markets found.")
        return

    vol24 = get_24h_stats()
    rank_map, info_map = rank_all_5m(markets)
    last_rank_refresh = time.time()

    send_telegram("✅ تم تشغيل مراقبة التريند الذكي.")

    while True:
        try:
            ts = time.time()

            # تحديث حجم 24h كل ~5 دقائق
            if int(ts) % 300 < SCAN_INTERVAL:
                vol24 = get_24h_stats()

            # تحديث ترتيب السوق كل RANK_REFRESH_SEC
            if ts - last_rank_refresh >= RANK_REFRESH_SEC:
                rank_map, info_map = rank_all_5m(markets)
                last_rank_refresh = ts

            # اختَر مجموعة عمل صغيرة: أعلى TOP_N_PREWATCH فقط لفحص مفصّل
            # (لكن مسار A يعتمد على قفزة الترتيب حتى خارج هذه المجموعة)
            prewatch = sorted(info_map.items(), key=lambda kv: rank_map.get(kv[0], 9999))[:TOP_N_PREWATCH]
            prewatch_markets = set(m for m,_ in prewatch)

            # امشِ على كامل السوق لتَرصُّد قفزات الترتيب (A)
            for m,(ch5, price_now, vspike) in info_map.items():
                sym = m.replace("-EUR","")
                if sym not in supported:
                    continue

                # سيولة يومية
                if vol24.get(m, 0.0) < MIN_DAILY_EUR:
                    continue

                # حدّث قمة 10د
                update_high10(m, price_now, ts)

                # مسار A: قفزة ترتيب + شروط خفيفة
                condA = (
                    is_rank_jump(m, rank_map.get(m, 9999), ts) and
                    ch5 is not None and ch5 >= 1.2 and
                    vspike is not None and vspike >= VOL_SPIKE_MULT
                )

                # المساران B و C نفحصهما فقط ضمن prewatch لتقليل الضغط
                condB = condC = False
                if m in prewatch_markets:
                    ok_step, _ = in_step_pattern(sym, price_now, ts)
                    condB = ok_step and rank_map.get(m, 9999) <= 30 and vspike >= VOL_SPIKE_MULT

                    ch15 = compute_15m_change(m)
                    condC = (ch15 is not None and ch15 >= 3.0 and is_breakout_local(m, price_now))

                if (condA or condB or condC) and should_alert(sym):
                    mark_alerted(sym)
                    msg = f"🚀 {sym} | 5m {ch5:+.2f}% | r#{rank_map.get(m, '?')} | spike×{vspike:.1f}"
                    try:
                        ch15 = compute_15m_change(m)
                        if ch15 is not None:
                            msg += f" | 15m {ch15:+.2f}%"
                    except:
                        pass
                    send_telegram(msg)
                    send_to_saqr(sym)
                    print("ALERT:", msg)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print("monitor error:", e)
            time.sleep(SCAN_INTERVAL)

# =========================
# 🌐 Healthcheck + Webhook
# =========================
@app.route("/", methods=["GET"])
def alive():
    return "Nems Trend bot is alive ✅", 200

@app.route("/webhook", methods=["POST"])
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        txt = (data.get("message", {}).get("text") or "").strip().lower()
        if not txt:
            return "ok", 200

        if txt in ("ابدأ", "start"):
            Thread(target=monitor_loop, daemon=True).start()
            send_telegram("✅ بدأ الرصد.")
            return "ok", 200

        elif txt in ("السجل", "log"):
            # اعرض أفضل 10 حالياً حسب 5m
            if not info_map:
                send_telegram("ℹ️ لا توجد بيانات بعد.")
                return "ok", 200
            top = sorted(info_map.items(), key=lambda kv: rank_map.get(kv[0], 9999))[:10]
            lines = ["📈 أفضل 10 (5m):"]
            for m,(ch,p,vsp) in top:
                lines.append(f"- {m}: {ch:+.2f}% | €{p:.5f} | spike≈{vsp:.1f}x | r#{rank_map.get(m,'?')}")
            send_telegram("\n".join(lines))
            return "ok", 200

        elif txt in ("مسح", "مسح الذاكرة", "reset"):
            reset_state()
            clear_redis_namespace()
            send_telegram("🧹 تم مسح الذاكرة ومفاتيح Redis الخاصة. جلسة جديدة نظيفة.")
            return "ok", 200

        elif txt.startswith("اعدادات") or txt.startswith("settings"):
            conf = {
                "SCAN_INTERVAL": SCAN_INTERVAL,
                "TOP_N_PREWATCH": TOP_N_PREWATCH,
                "VOL_SPIKE_MULT": VOL_SPIKE_MULT,
                "MIN_DAILY_EUR": MIN_DAILY_EUR,
                "BUY_COOLDOWN_SEC": BUY_COOLDOWN_SEC,
                "RANK_JUMP_STEPS": RANK_JUMP_STEPS,
                "RANK_REFRESH_SEC": RANK_REFRESH_SEC,
                "STEP_WINDOW_SEC": STEP_WINDOW_SEC,
                "STEP_PCT": STEP_PCT,
                "STEP_BACKDRIFT": STEP_BACKDRIFT,
                "BREAKOUT_LOOKBACK_M": BREAKOUT_LOOKBACK_M,
                "BREAKOUT_BUFFER_PCT": BREAKOUT_BUFFER_PCT,
                "CLEAN_ON_BOOT": CLEAN_ON_BOOT,
                "STATE_TTL_SEC": STATE_TTL_SEC,
                "GC_INTERVAL_SEC": GC_INTERVAL_SEC,
                "REDIS_NS": REDIS_NS,
            }
            send_telegram("⚙️ الإعدادات:\n" + json.dumps(conf, ensure_ascii=False, indent=2))
            return "ok", 200

        return "ok", 200

    except Exception as e:
        print("⚠️ Webhook error:", e)
        return "ok", 200

# =========================
# 🚀 الإقلاع
# =========================
def boot():
    if CLEAN_ON_BOOT:
        print("🧹 Fresh boot: clearing memory & redis namespace…")
        reset_state()
        clear_redis_namespace()
    Thread(target=gc_loop, daemon=True).start()
    # ابدأ المراقبة تلقائيًا عند الإقلاع (يمكنك تعطيلها إن أردت)
    Thread(target=monitor_loop, daemon=True).start()

if __name__ == "__main__":
    boot()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)