import os
import time
import redis
import requests
import threading
from flask import Flask, request
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

load_dotenv()
app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/webhook"

HISTORY_SECONDS = 30 * 60     # 30 دقيقة = 1800 ثانية
FETCH_INTERVAL = 5            # لا تغيير
COOLDOWN = 60                 # لا تغيير
THREAD_COUNT = 4  # عدد الخيوط

def fetch_all_prices():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/price")
        data = res.json()
        return {
            item["market"].replace("-EUR", ""): float(item["price"])
            for item in data if item["market"].endswith("-EUR")
        }
    except Exception as e:
        print("❌ فشل جلب الأسعار:", e)
        return {}

def store_price(symbol, price):
    now = int(time.time())
    cutoff = now - HISTORY_SECONDS
    try:
        key = f"prices:{symbol}"
        r.zadd(key, {price: now})
        r.zremrangebyscore(key, 0, cutoff)
    except Exception as e:
        print(f"❌ خطأ في تخزين {symbol}:", e)

def store_prices_threaded(prices):
    symbols = list(prices.keys())
    chunks = [symbols[i::THREAD_COUNT] for i in range(THREAD_COUNT)]

    def process_chunk(chunk):
        for symbol in chunk:
            store_price(symbol, prices[symbol])

    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
        executor.map(process_chunk, chunks)

def get_price_at(symbol, target_time):
    key = f"prices:{symbol}"
    results = r.zrangebyscore(key, target_time - 3, target_time + 3, withscores=True)
    if results:
        return float(results[0][0])
    else:
        # fallback: خذ أقرب سعر قبل الوقت المحدد
        fallback = r.zrevrangebyscore(key, target_time, 0, start=0, num=1, withscores=True)
        if fallback:
            print(f"⚠️ استخدام fallback لـ {symbol} عند {target_time} → {fallback[0][1]}")
            return float(fallback[0][0])
        else:
            print(f"⚠️ لا يوجد سعر لـ {symbol} في {target_time}")
    return None

def notify_buy(symbol):
    last_key = f"alerted:{symbol}"
    if r.get(last_key):
        return
    msg = f"اشتري {symbol}"
    try:
        r.set(last_key, "1", ex=COOLDOWN)
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": f"🚀 إشارة شراء: {msg}"}
        )
        print(f"🚀 إشارة شراء: {msg}")
    except Exception as e:
        print(f"❌ فشل إرسال الإشارة لـ {symbol}:", e)

def analyze_symbol(symbol):
    now = int(time.time())
    current = get_price_at(symbol, now)
    if not current:
        return

    checks = {
        "5s": now - 5,
        "10s": now - 10,
        "60s": now - 60,
        "180s": now - 180,
        "300s": now - 300,
    }

    for label, ts in checks.items():
        old_price = get_price_at(symbol, ts)
        if not old_price:
            print(f"❌ {symbol}: لا يوجد سعر في {label} (target={ts})")
            continue

        diff_sec = now - ts
        change = ((current - old_price) / old_price) * 100
        print(f"🔍 {symbol}: {label} | الآن={current:.6f}, قبل={old_price:.6f}, تغير={change:.2f}% خلال {diff_sec}ث")

        if label == "5s" and change >= 2:
            notify_buy(symbol)
        elif label == "10s" and change >= 3:
            notify_buy(symbol)
        elif label == "60s" and change >= 5:
            notify_buy(symbol)
        elif label == "180s" and change >= 8:
            notify_buy(symbol)
        elif label == "300s" and change >= 10:
            notify_buy(symbol)

def analyzer_loop():
    while True:
        keys = r.keys("prices:*")
        symbols = [k.decode().split(":")[1] for k in keys]
        for sym in symbols:
            try:
                analyze_symbol(sym)
            except Exception as e:
                print(f"❌ تحليل {sym}:", e)
        time.sleep(FETCH_INTERVAL)

def collector_loop():
    while True:
        prices = fetch_all_prices()
        if prices:
            store_prices_threaded(prices)
            print(f"✅ تم تخزين {len(prices)} عملة.")
        time.sleep(FETCH_INTERVAL)

def print_summary():
    keys = r.keys("prices:*")
    symbols = [k.decode().split(":")[1] for k in keys]
    now = int(time.time())
    changes_5min = []
    changes_10min = []

    for sym in symbols:
        current = get_price_at(sym, now)
        ago_5 = get_price_at(sym, now - 300)
        ago_10 = get_price_at(sym, now - 600)

        if current and ago_5:
            change = ((current - ago_5) / ago_5) * 100
            changes_5min.append((sym, round(change, 2)))

        if current and ago_10:
            change = ((current - ago_10) / ago_10) * 100
            changes_10min.append((sym, round(change, 2)))
        
        # طباعة للتحقق
        print(f"📊 {sym}: الآن={current}, قبل5د={ago_5}, قبل10د={ago_10}")

    top5_5m = sorted(changes_5min, key=lambda x: x[1], reverse=True)[:5]
    top5_10m = sorted(changes_10min, key=lambda x: x[1], reverse=True)[:5]

    text = f"🧠 عدد العملات المخزنة: {len(symbols)}\n\n"
    text += "📈 أعلى 5 خلال 5 دقائق:\n"
    for sym, ch in top5_5m:
        text += f"- {sym}: {ch:.2f}%\n"
    text += "\n📈 أعلى 5 خلال 10 دقائق:\n"
    for sym, ch in top5_10m:
        text += f"- {sym}: {ch:.2f}%\n"

    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}
        )
    except Exception as e:
        print("❌ فشل إرسال السجل:", e)

@app.route("/")
def home():
    return "Sniper bot is alive ✅"

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    if "message" not in data:
        return "no message", 200

    text = data["message"].get("text", "").strip().lower()
    if "السجل" in text:
        print_summary()

    return "ok", 200

def clear_old_prices():
    keys = r.keys("prices:*")
    for k in keys:
        r.delete(k)
    print("🧹 تم حذف جميع الأسعار القديمة من Redis.")
    
if __name__ == "__main__":
    clear_old_prices()  # 🧹 حذف البيانات القديمة

    threading.Thread(target=collector_loop, daemon=True).start()

    # ⏳ انتظر أول دفعة أسعار قبل بدء التحليل
    while not r.keys("prices:*"):
        print("⏳ في انتظار أول دفعة أسعار...")
        time.sleep(1)

    threading.Thread(target=analyzer_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))