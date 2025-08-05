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
SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/"

HISTORY_SECONDS = 30 * 60
FETCH_INTERVAL = 5
COOLDOWN = 60

# 🟢 جلب الأسعار من Bitvavo
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

# 🟢 تخزين الأسعار في Redis
def store_prices(prices):
    now = int(time.time())
    cutoff = now - HISTORY_SECONDS
    for symbol, price in prices.items():
        key = f"prices:{symbol}"
        r.zadd(key, {price: now})
        r.zremrangebyscore(key, 0, cutoff)
    print(f"✅ تم تخزين {len(prices)} عملة. مثال: {list(prices.items())[:3]}")

# 🟢 استخراج السعر القديم
def get_price_at(symbol, seconds_ago):
    target = int(time.time()) - seconds_ago
    key = f"prices:{symbol}"
    result = r.zrangebyscore(key, target - 2, target + 2, withscores=False)
    if result:
        return float(result[0])
    return None

# 🟢 إرسال إشارة شراء
def notify_buy(symbol):
    last_key = f"alerted:{symbol}"
    if r.get(last_key):
        return
    msg = f"اشتري {symbol}"
    r.set(last_key, "1", ex=COOLDOWN)
    try:
        saqar = requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
        tg = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": f"🚀 إشارة شراء: {msg}"})
        print(">> صقر:", saqar.status_code, saqar.text)
        print(">> تلغرام:", tg.status_code, tg.text)
    except Exception as e:
        print("❌ فشل إرسال الإشارة:", e)

# 🟢 تحليل العملات واحدة واحدة
def analyze_symbol(symbol):
    current = get_price_at(symbol, 0)
    if not current or current == 0:
        return

    deltas = {}
    intervals = [5, 10, 60, 180, 300]
    thresholds = {5: 2.0, 10: 3.0, 60: 4.0}
    for sec in intervals:
        past = get_price_at(symbol, sec)
        if past and past > 0:
            change = ((current - past) / past) * 100
            deltas[sec] = round(change, 3)

    print(f"🔍 تحليل {symbol}: 🟧 السعر الحالي: {current}")
    for sec, change in deltas.items():
        print(f"⏱️ {sec}s: {change}% (مقارنة بـ {get_price_at(symbol, sec)})")

        if change >= thresholds[sec]:
            print(f"🚨 انفجار {symbol}: +{change}% خلال {sec}s")
            notify_buy(symbol)
            break

# 🧠 خيط التحليل المستمر
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

# 🧠 خيط التخزين المستمر
def collector_loop():
    while True:
        prices = fetch_all_prices()
        if prices:
            store_prices(prices)
        time.sleep(FETCH_INTERVAL)

# 🧠 عرض السجل لأقوى العملات
def print_summary():
    keys = r.keys("prices:*")
    symbols = [k.decode().split(":")[1] for k in keys]
    now = int(time.time())
    changes_5min = []
    changes_10min = []

    for sym in symbols:
        current = get_price_at(sym, 0)
        ago_5 = get_price_at(sym, 300)
        ago_10 = get_price_at(sym, 600)

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

# 🚀 Flask Webhook
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