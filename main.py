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
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")

HISTORY_SECONDS = 30 * 60  # 30 دقيقة
FETCH_INTERVAL = 5
COOLDOWN = 60  # ثانية لكل عملة

# 🧠 قراءة السعر من Redis عند زمن معيّن
def get_price_at(symbol, target_time):
    key = f"prices:{symbol}"
    results = r.zrangebyscore(key, target_time - 2, target_time + 2, withscores=True)
    if results:
        return float(results[0][0])
    return None

# 🚀 إرسال إشارة شراء إلى صقر وتلغرام
def notify_buy(symbol):
    last_key = f"alerted:{symbol}"
    if r.get(last_key):
        return
    msg = f"اشتري {symbol}"
    r.set(last_key, "1", ex=COOLDOWN)

    try:
        # إلى صقر
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
        # إلى تلغرام
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": f"🚀 انفجار {symbol}"}
        )
        print("🚀", msg)
    except Exception as e:
        print(f"❌ فشل إرسال الإشعار لـ {symbol}: {e}")

# 🔍 تحليل عملة واحدة
def analyze_symbol(symbol):
    now = int(time.time())
    current = get_price_at(symbol, now)
    if not current:
        return

    checks = {
        "5s": (now - 5, 2),
        "10s": (now - 10, 3),
        "60s": (now - 60, 5),
        "180s": (now - 180, 8),
        "300s": (now - 300, 10),
    }

    for label, (past_time, threshold) in checks.items():
        past = get_price_at(symbol, past_time)
        if not past:
            continue
        change = ((current - past) / past) * 100
        if change >= threshold:
            notify_buy(symbol)
            break

# 🔁 تحليل مستمر
def analyzer_loop():
    while True:
        keys = r.keys("prices:*")
        symbols = [k.decode().split(":")[1] for k in keys]
        for symbol in symbols:
            try:
                analyze_symbol(symbol)
            except Exception as e:
                print(f"❌ {symbol}:", e)
        time.sleep(FETCH_INTERVAL)

# 💾 تخزين السعر كل 5 ثواني
def fetch_and_store_loop():
    while True:
        try:
            res = requests.get("https://api.bitvavo.com/v2/ticker/price")
            data = res.json()
            now = int(time.time())
            cutoff = now - HISTORY_SECONDS

            count = 0
            for item in data:
                if item["market"].endswith("-EUR"):
                    symbol = item["market"].replace("-EUR", "")
                    price = float(item["price"])
                    key = f"prices:{symbol}"
                    r.zadd(key, {price: now})
                    r.zremrangebyscore(key, 0, cutoff)
                    count += 1

            print(f"✅ تخزين {count} عملة عند {now}")
        except Exception as e:
            print("❌ فشل جلب الأسعار:", e)

        time.sleep(FETCH_INTERVAL)

# 📊 حساب التغير خلال دقائق
def get_top_movers(minutes=5, top_n=5):
    now = int(time.time())
    result = []
    keys = r.keys("prices:*")
    for key in keys:
        symbol = key.decode().split(":")[1]
        current = get_price_at(symbol, now)
        past = get_price_at(symbol, now - minutes * 60)
        if current and past:
            change = ((current - past) / past) * 100
            result.append((symbol, round(change, 2)))
    result.sort(key=lambda x: x[1], reverse=True)
    return result[:top_n]

# 📩 أمر /السجل من تلغرام
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    message = data.get("message", {})
    text = message.get("text", "").lower()
    if "السجل" in text:
        total = len(r.keys("prices:*"))
        movers_5 = get_top_movers(5)
        movers_10 = get_top_movers(10)

        response = f"📊 العملات المخزنة: {total}\n"
        response += "\n🔥 أفضل 5 خلال 5 دقائق:\n"
        for sym, ch in movers_5:
            response += f"- {sym}: {ch:.2f}%\n"
        response += "\n⚡️ أفضل 5 خلال 10 دقائق:\n"
        for sym, ch in movers_10:
            response += f"- {sym}: {ch:.2f}%\n"

        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": response}
            )
        except Exception as e:
            print("❌ فشل إرسال السجل:", e)

    return "OK", 200

# 🚀 التشغيل
if __name__ == "__main__":
    threading.Thread(target=fetch_and_store_loop, daemon=True).start()
    threading.Thread(target=analyzer_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)