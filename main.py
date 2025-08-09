import os, time, json, math, requests, redis
from flask import Flask, request
from collections import deque, defaultdict
from threading import Thread, Lock
from dotenv import load_dotenv

load_dotenv()

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", 10))    # كل كم ثانية نفحص
TOP_N              = int(os.getenv("TOP_N", 10))            # نراقب دخول جديد إلى Top N بفريم 5m
STEP_WINDOW_SEC    = int(os.getenv("STEP_WINDOW_SEC", 180)) # نافذة نمط 1%+1% (ثواني)
STEP_PCT           = float(os.getenv("STEP_PCT", 1.0))      # كل خطوة 1%
VOL_SPIKE_MULT     = float(os.getenv("VOL_SPIKE_MULT", 1.8))# مضاعفة حجم الدقيقة
BUY_COOLDOWN_SEC   = int(os.getenv("BUY_COOLDOWN_SEC", 900))# منع تكرار الإشعار لنفس العملة
MIN_DAILY_EUR      = float(os.getenv("MIN_DAILY_EUR", 30000)) # حد أدنى لقيمة تداول يومية €

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")  # مثال: https://your-app/webhook
REDIS_URL = os.getenv("REDIS_URL")

app = Flask(__name__)
r = redis.from_url(REDIS_URL) if REDIS_URL else None
lock = Lock()

# حالات داخلية
step_state = {}                      # symbol -> {"base":price, "p1":(price,t), "p2":(price,t)}
price_cache = {}                     # symbol-EUR -> آخر سعر
last_alert   = {}                    # symbol -> timestamp
watch_topset = set()                 # آخر مجموعة TopN (5m)
vol_cache    = defaultdict(lambda: deque(maxlen=20))  # لكل عملة: [ (ts, close, vol_1m) ] حتى 20 دقيقة
supported = set()

def send_telegram(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text}, timeout=8)
    except Exception as e:
        print("TG error:", e)

def send_to_saqr(symbol):
    try:
        payload = {"message": {"text": f"اشتري {symbol}"}}
        resp = requests.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        print("Send Saqr:", resp.status_code, resp.text[:180])
    except Exception as e:
        print("Saqr error:", e)

# ---------- Bitvavo helpers ----------
def get_markets_eur():
    global supported
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets", timeout=10)
        data = res.json()
        supported = {m["market"].replace("-EUR","") for m in data if m.get("market","").endswith("-EUR")}
        return [m["market"] for m in data if m.get("market","").endswith("-EUR")]
    except Exception as e:
        print("markets error:", e)
        return []

def get_candles(market, interval="1m", limit=20):
    # [ [time, open, high, low, close, volume], ... ]
    try:
        res = requests.get(f"https://api.bitvavo.com/v2/{market}/candles?interval={interval}&limit={limit}", timeout=10)
        return res.json()
    except Exception as e:
        print("candles error:", market, e)
        return []

def get_ticker_price(market):
    try:
        res = requests.get(f"https://api.bitvavo.com/v2/ticker/price?market={market}", timeout=6).json()
        return float(res.get("price", 0) or 0)
    except Exception:
        return 0.0

def get_24h_stats():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/24h", timeout=10).json()
        # نرجع dict: 'ADA-EUR' -> (volumeEUR)
        out = {}
        for row in res:
            market = row.get("market")
            if not market or not market.endswith("-EUR"): continue
            vol_eur = float(row.get("volume", 0)) * float(row.get("last", 0))
            out[market] = vol_eur
        return out
    except Exception:
        return {}

# ---------- Logic ----------
def compute_5m_change(market):
    c = get_candles(market, "1m", 6)  # آخر 6 شمعات (للاحتياط)
    if len(c) < 5: return None, None, None
    closes = [float(x[4]) for x in c]
    vols   = [float(x[5]) for x in c]
    p_now = closes[-1]
    p_5m  = closes[0]
    change = (p_now - p_5m) / p_5m * 100 if p_5m > 0 else 0
    # حجم الدقيقة الأخيرة مقابل متوسط 15 دقيقة
    c15 = get_candles(market, "1m", 16)
    if len(c15) < 3:
        v_spike = 1.0
    else:
        vol_last = float(c15[-1][5])
        avg_prev = sum(float(x[5]) for x in c15[:-1]) / max(len(c15)-1, 1)
        v_spike = (vol_last / avg_prev) if avg_prev > 0 else 1.0
    return change, p_now, v_spike

def in_step_pattern(sym, price_now, ts):
    """
    نحقق نمط 1% + 1% خلال STEP_WINDOW_SEC.
    - base يتحدد عند أول ظهور.
    - إذا وصل +1% -> p1
    - من p1، إذا ارتفع +1% إضافية -> نجاح
    - يتم reset إذا تعدّى الوقت أو هبط تحت base (بشكل كبير).
    """
    st = step_state.get(sym)
    if st is None:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False, "init"

    # reset إذا تجاوزنا النافذة
    if ts - st["t0"] > STEP_WINDOW_SEC:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False, "window_reset"

    base = st["base"]
    if base <= 0:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False, "bad_base"

    step_pct_now = (price_now - base) / base * 100

    # لو هبط كتير، أعد الضبط
    if step_pct_now < -0.5:
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
            # نجاح
            step_state[sym] = {"base": price_now, "p1": None, "t0": ts}  # reset بعد النجاح
            return True, "step2_ok"
        return False, "waiting_step2"

def should_alert(sym):
    ts = time.time()
    last = last_alert.get(sym, 0)
    return (ts - last) >= BUY_COOLDOWN_SEC

def mark_alerted(sym):
    last_alert[sym] = time.time()
    if r:
        r.setex(f"alerted:{sym}", BUY_COOLDOWN_SEC, 1)

def refresh_top_set(markets):
    """
    نحسب TopN (5m) عبر تغيير 5m لكل أزواج EUR ونرجّع المجموعة والقيم.
    """
    changes = []
    for m in markets:
        ch, p, vsp = compute_5m_change(m)
        if ch is None: continue
        changes.append((m, ch, p, vsp))
    changes.sort(key=lambda x: x[1], reverse=True)
    top = changes[:TOP_N]
    return set(m for m, _, _, _ in top), {m: (ch, p, vsp) for m, ch, p, vsp in top}

def monitor_loop():
    markets = get_markets_eur()
    vol24_dict = get_24h_stats()
    if not markets:
        print("No markets found.")
        return

    global watch_topset
    # بداية: كوِّن مجموعة الـ TopN
    watch_topset, top_map = refresh_top_set(markets)

    while True:
        try:
            ts = time.time()
            # تحديث حجم 24h كل ~5 دقائق
            if int(ts) % 300 < SCAN_INTERVAL:
                vol24_dict = get_24h_stats()

            new_topset, top_map = refresh_top_set(markets)

            # العملات التي دخلت التوب حديثًا
            newly_in = new_topset - watch_topset
            watch_topset = new_topset

            for m in new_topset:  # نراقب كل التوب، ونؤكد الشروط
                sym = m.replace("-EUR", "")
                if sym not in supported:  # حماية
                    continue

                ch5, price_now, vspike = top_map.get(m, (None, None, None))
                if ch5 is None or price_now is None:
                    continue

                # سجل للـ step pattern
                ok, stage = in_step_pattern(sym, price_now, ts)

                # شرط حجم يومي أدنى
                vol_eur_24h = vol24_dict.get(m, 0.0)
                if vol_eur_24h < MIN_DAILY_EUR:
                    # تجاهل سيولة ضعيفة
                    continue

                # شروط التنبيه:
                # 1) دخل حديثًا التوب أو stage أنهى step2
                # 2) volume spike قوي
                if (sym in {x.replace("-EUR","") for x in newly_in} or ok) and vspike >= VOL_SPIKE_MULT:
                    if should_alert(sym):
                        mark_alerted(sym)
                        msg = f"🚀 {sym} ترند مبكر | 5m: {ch5:+.2f}% | spike×{vspike:.1f}"
                        send_telegram(msg)
                        if SAQAR_WEBHOOK:
                            send_to_saqr(sym)
                        print("ALERT:", msg)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print("monitor error:", e)
            time.sleep(SCAN_INTERVAL)

# ============== Healthcheck ==============
@app.route("/", methods=["GET"])
def alive():
    return "Nems bot is alive ✅", 200

# ============== Webhook (تيليجرام) ==============
# استقبل "/" و "/webhook" لتفادي عدم التطابق
@app.route("/webhook", methods=["POST"])
@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        txt = (data.get("message", {}).get("text") or "").strip().lower()

        if not txt:
            return "ok", 200

        if txt == "السجل":
            top_list = list(watch_topset)[:TOP_N]
            lines = [f"📈 مراقبة Top{TOP_N} 5m:"]
            for m in top_list:
                ch, p, vsp = compute_5m_change(m)
                if ch is None:
                    continue
                lines.append(f"- {m}: {ch:+.2f}% | €{p:.5f} | spike≈{vsp:.1f}x")
            send_telegram("\n".join(lines))
            return "ok", 200

        elif txt.startswith("ابدأ"):
            Thread(target=monitor_loop, daemon=True).start()
            send_telegram("✅ تم تشغيل مراقبة التريند المبكر.")
            return "ok", 200

        # أوامر أخرى...
        return "ok", 200

    except Exception as e:
        print("⚠️ Webhook error:", e)
        # نرجّع 200 حتى ما يعتبره تيليجرام فشل
        return "ok", 200

# ============== تشغيل ==============
# ============== تشغيل ==============
if __name__ == "__main__":
    # شغّل المونيتور بخيط منفصل
    Thread(target=monitor_loop, daemon=True).start()

    # لازم نستخدم المنفذ الذي يرسله Railway
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)