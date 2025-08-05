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
HISTORY_SECONDS = 20 * 60      # ⏳ احتفاظ بالبيانات 20 دقيقة
FETCH_INTERVAL = 30            # ⏱️ تخزين كل 30 ثانية
COOLDOWN = 60                  # 🧊 تجميد الإشعارات

# 🔁 جلب الأسعار
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

# 💾 تخزين الأسعار في Redis
def store_prices(prices):
    now = int(time.time())
    cutoff = now - HISTORY_SECONDS
    for symbol, price in prices.items():
        key = f"prices:{symbol}"
        r.zadd(key, {price: now})
        r.zremrangebyscore(key, 0, cutoff)
    print(f"✅ تم تخزين {len(prices)} عملة.")

# 📦 جلب سعر قديم
def get_price_at(symbol, seconds_ago):
    target = int(time.time()) - seconds_ago
    key = f"prices:{symbol}"
    result = r.zrangebyscore(key, target - 2, target + 2, withscores=False)
    if result:
        return float(result[0])
    return None

# 🚨 إشعار شراء
def notify_buy(symbol, percent, tag):
    last_key = f"alerted:{symbol}"
    if r.get(last_key):
        return
    r.set(last_key, "1", ex=COOLDOWN)
    msg = f"اشتري {symbol}"

    try:
        # إلى صقر
        saqar = requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
        print(">> صقر:", saqar.status_code, saqar.text)

        # إلى تلغرام
        text = f"🚀 {msg} (+{percent:.2f}%) #{tag}"
        tg = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text})
        print(">> تلغرام:", tg.status_code, tg.text)
    except Exception as e:
        print("❌ فشل إرسال الإشارة:", e)

# 🔍 تحليل عملة
def analyze_symbol(symbol):
    current = get_price_at(symbol, 0)
    if not current or current == 0:
        return

    checks = [(30, 2.5), (60, 3.0), (120, 4.0)]

    for sec, threshold in checks:
        past = get_price_at(symbol, sec)
        if past and past > 0:
            change = ((current - past) / past) * 100
            if change >= threshold:
                print(f"🚨 {symbol}: +{change:.2f}% خلال {sec}s")
                notify_buy(symbol, change, sec)
                break

# 🧠 تحليل كل العملات
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

# ⏺️ تخزين كل الأسعار
def collector_loop():
    while True:
        prices = fetch_all_prices()
        if prices:
            store_prices(prices)
        time.sleep(FETCH_INTERVAL)

# 📊 سجل أفضل العملات
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

    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text})

# 🚀 نقطة البداية + Webhook
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
    # 🧹 تنظيف البيانات القديمة فقط
    for key in r.scan_iter("prices:*"):
        r.delete(key)
    for key in r.scan_iter("alerted:*"):
        r.delete(key)
    print("🧹 تم حذف أسعار العملات والإشعارات السابقة من Redis.")

    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=analyzer_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))