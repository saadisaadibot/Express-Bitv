import os
import time
import redis
import requests
import threading
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/webhook"

HISTORY_SECONDS = 2 * 60 * 60
FETCH_INTERVAL = 5
COOLDOWN = 60

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

def store_prices(prices):
    now = int(time.time())
    cutoff = now - HISTORY_SECONDS
    for symbol, price in prices.items():
        key = f"prices:{symbol}"
        r.zadd(key, {price: now})
        r.zremrangebyscore(key, 0, cutoff)

def get_price_at(symbol, target_time):
    key = f"prices:{symbol}"
    results = r.zrangebyscore(key, target_time - 2, target_time + 2, withscores=True)
    if results:
        return float(results[0][0])
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
        "5s": get_price_at(symbol, now - 5),
        "10s": get_price_at(symbol, now - 10),
        "60s": get_price_at(symbol, now - 60),
        "180s": get_price_at(symbol, now - 180),
        "300s": get_price_at(symbol, now - 300),
    }

    for label, old_price in checks.items():
        if not old_price:
            continue
        change = ((current - old_price) / old_price) * 100
        if label == "5s" and change >= 0.1:
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
            store_prices(prices)
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

    top5_5m = sorted(changes_5min, key=lambda x: x[1], reverse=True)[:5]
    top5_10m = sorted(changes_10min, key=lambda x: x[1], reverse=True)[:5]

    text = f"🧠 عدد العملات المخزنة: {len(symbols)}\n\n"
    text += "📈 أعلى 5 خلال 5 دقائق:\n"
    for sym, ch in top5_5m:
        text += f"- {sym}: {ch:.2f}%\n"
    text += "\n📈 أعلى 5 خلال 10 دقائق:\n"
    for sym, ch in top5_10m:
        text += f"- {sym}: {ch:.2f}%\n"

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text}
    )

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

if __name__ == "__main__":
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=analyzer_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))