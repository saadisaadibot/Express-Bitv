import os, time, json, requests, redis
from flask import Flask, request
from collections import deque, defaultdict
from threading import Thread, Lock
from dotenv import load_dotenv

load_dotenv()

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", 10))     # كل كم ثانية نفحص
TOP_N              = int(os.getenv("TOP_N", 20))             # عدد العملات المراقبة
STEP_WINDOW_SEC    = int(os.getenv("STEP_WINDOW_SEC", 180))  # نافذة نمط 1%+1%
STEP_PCT           = float(os.getenv("STEP_PCT", 1.0))       # كل خطوة 1%
VOL_SPIKE_MULT     = float(os.getenv("VOL_SPIKE_MULT", 1.8)) # مضاعفة حجم الدقيقة
BUY_COOLDOWN_SEC   = int(os.getenv("BUY_COOLDOWN_SEC", 900)) # منع تكرار الإشعار لنفس العملة
MIN_DAILY_EUR      = float(os.getenv("MIN_DAILY_EUR", 30000))# حد أدنى لقيمة تداول يومية €

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")
REDIS_URL = os.getenv("REDIS_URL")

app = Flask(__name__)
r = redis.from_url(REDIS_URL) if REDIS_URL else None
lock = Lock()

# حالات داخلية
step_state   = {}
last_alert   = {}
watch_set    = set()
supported    = set()

# ========== إرسال رسائل ==========
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

# ========== Bitvavo ==========
def get_markets_eur():
    global supported
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets", timeout=10).json()
        supported = {m["market"].replace("-EUR", "") for m in res if m["market"].endswith("-EUR")}
        return [m["market"] for m in res if m["market"].endswith("-EUR")]
    except Exception as e:
        print("markets error:", e)
        return []

def get_candles(market, interval="5m", limit=4):
    try:
        res = requests.get(f"https://api.bitvavo.com/v2/{market}/candles?interval={interval}&limit={limit}", timeout=10)
        return res.json()
    except:
        return []

def get_24h_stats():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/24h", timeout=10).json()
        out = {}
        for row in res:
            if row["market"].endswith("-EUR"):
                vol_eur = float(row["volume"]) * float(row["last"])
                out[row["market"]] = vol_eur
        return out
    except:
        return {}

# ========== تحليل الفريمات ==========
def compute_change(market, interval):
    candles = get_candles(market, interval, 4)
    if len(candles) < 2:
        return None, None, None
    p_now = float(candles[-1][4])
    p_prev = float(candles[0][4])
    change = (p_now - p_prev) / p_prev * 100 if p_prev > 0 else 0
    # حجم الدقيقة الأخيرة مقارنة بمتوسط سابق
    vol_last = float(candles[-1][5])
    avg_prev = sum(float(c[5]) for c in candles[:-1]) / max(len(candles)-1, 1)
    vspike = vol_last / avg_prev if avg_prev > 0 else 1.0
    return change, p_now, vspike

def get_top_from_interval(markets, interval):
    changes = []
    for m in markets:
        ch, p, vsp = compute_change(m, interval)
        if ch is None: continue
        changes.append((m, ch, p, vsp))
    changes.sort(key=lambda x: x[1], reverse=True)
    return changes[:TOP_N]

# ========== منطق النمط ==========
def in_step_pattern(sym, price_now, ts):
    st = step_state.get(sym)
    if st is None:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False
    if ts - st["t0"] > STEP_WINDOW_SEC:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False
    base = st["base"]
    step_pct_now = (price_now - base) / base * 100
    if step_pct_now < -0.5:
        step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
        return False
    if not st["p1"]:
        if step_pct_now >= STEP_PCT:
            st["p1"] = (price_now, ts)
        return False
    else:
        p1_price, _ = st["p1"]
        if (price_now - p1_price) / p1_price * 100 >= STEP_PCT:
            step_state[sym] = {"base": price_now, "p1": None, "t0": ts}
            return True
    return False

def should_alert(sym):
    return (time.time() - last_alert.get(sym, 0)) >= BUY_COOLDOWN_SEC

def mark_alerted(sym):
    last_alert[sym] = time.time()

# ========== المراقبة ==========
def monitor_loop():
    global watch_set
    markets = get_markets_eur()
    vol24_dict = get_24h_stats()
    if not markets:
        print("No markets found")
        return

    while True:
        try:
            ts = time.time()
            if int(ts) % 300 < SCAN_INTERVAL:
                vol24_dict = get_24h_stats()

            # جمع التوب من كل الفريمات
            all_changes = []
            for interval in ["5m", "15m", "30m", "1h"]:
                all_changes.extend(get_top_from_interval(markets, interval))

            # إزالة التكرارات مع الاحتفاظ بالأقوى
            merged = {}
            for m, ch, p, vsp in sorted(all_changes, key=lambda x: x[1], reverse=True):
                if m not in merged:
                    merged[m] = (ch, p, vsp)

            # اختيار TOP_N النهائية
            final_list = list(merged.items())[:TOP_N]
            watch_set = {m for m, _ in final_list}

            for m, (ch, price_now, vspike) in final_list:
                sym = m.replace("-EUR", "")
                if sym not in supported:
                    continue
                if vol24_dict.get(m, 0) < MIN_DAILY_EUR:
                    continue

                if in_step_pattern(sym, price_now, ts) and vspike >= VOL_SPIKE_MULT:
                    if should_alert(sym):
                        mark_alerted(sym)
                        msg = f"🚀 {sym} ترند متعدد | {ch:+.2f}% | spike×{vspike:.1f}"
                        send_telegram(msg)
                        send_to_saqr(sym)
                        print("ALERT:", msg)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print("monitor error:", e)
            time.sleep(SCAN_INTERVAL)

# ========== Flask Routes ==========
@app.route("/", methods=["GET"])
def alive():
    return "Nems bot is alive ✅", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    txt = (data.get("message", {}).get("text") or "").strip().lower()
    if txt == "السجل":
        lines = [f"📊 مراقبة {len(watch_set)} عملة:"]
        for m in watch_set:
            ch, p, vsp = compute_change(m, "5m")
            lines.append(f"- {m}: {ch:+.2f}% | €{p:.5f} | spike≈{vsp:.1f}x")
        send_telegram("\n".join(lines))
    elif txt.startswith("ابدأ"):
        Thread(target=monitor_loop, daemon=True).start()
        send_telegram("✅ بدأ المراقبة على الفريمات المتعددة.")
    return "ok", 200

# ========== تشغيل ==========
if __name__ == "__main__":
    Thread(target=monitor_loop, daemon=True).start()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)