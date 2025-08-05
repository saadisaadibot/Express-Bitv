import os
import time
import redis
import requests
import threading
from flask import Flask, request
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# 📦 تحميل الإعدادات
load_dotenv()
app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK")

# ⚙️ إعدادات التخزين والتحليل
EXPIRATION_SECONDS = 1800  # 30 دقيقة
COLLECT_INTERVAL = 5       # كل 5 ثواني
THREAD_COUNT = 4           # عدد الخيوط
COOLDOWN = 60              # عدم تكرار الإشعار خلال دقيقة

# 🟢 جلب كل العملات المتوفرة على Bitvavo
def fetch_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets")
        data = res.json()
        return [m["market"].replace("-EUR", "").upper() for m in data if m["market"].endswith("-EUR")]
    except Exception as e:
        print("❌ خطأ في fetch_symbols:", e)
        return []

# 💾 تخزين السعر في Redis
def store_price(symbol):
    try:
        url = f"https://api.bitvavo.com/v2/ticker/price?market={symbol}-EUR"
        res = requests.get(url, timeout=3)
        data = res.json()
        price = float(data["price"])
        now = int(time.time())
        key = f"prices:{symbol}"

        # تخزين فقط إذا تغير السعر بوضوح
        latest = r.zrevrange(key, 0, 0, withscores=True)
        if latest:
            old_price = float(latest[0][0])
            if abs(price - old_price) / old_price < 0.0001:
                return

        r.zadd(key, {str(price): now})
        r.zremrangebyscore(key, 0, now - EXPIRATION_SECONDS)
    except Exception as e:
        print(f"❌ خطأ في {symbol}:", e)

# 🧵 تخزين أسعار مجموعة من العملات
def store_prices_batch(symbols):
    for symbol in symbols:
        store_price(symbol)

# 📦 تشغيل التخزين بخيوط متعددة
def collector_loop():
    while True:
        symbols = fetch_symbols()
        if not symbols:
            time.sleep(10)
            continue
        chunks = [symbols[i::THREAD_COUNT] for i in range(THREAD_COUNT)]
        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
            executor.map(store_prices_batch, chunks)
        print(f"✅ تم تخزين {len(symbols)} عملة.")
        time.sleep(COLLECT_INTERVAL)

# 📈 قراءة السعر من Redis في وقت معين
def get_price_at(symbol, timestamp):
    key = f"prices:{symbol}"
    res = r.zrangebyscore(key, timestamp - 2, timestamp + 2, withscores=True)
    if res:
        return float(res[0][0])
    return None

# 🚀 إرسال إشارة شراء إلى صقر وتلغرام
def notify_buy(symbol):
    key = f"alerted:{symbol}"
    if r.get(key):
        return
    msg = f"اشتري {symbol}"
    r.set(key, "1", ex=COOLDOWN)

    # إلى صقر
    try:
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
    except: pass

    # إلى تلغرام
    try:
        text = f"🚀 انفجار {symbol}"
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text})
    except: pass

    print("🚀", msg)

# 🧠 تحليل التغير السعري لعملة واحدة
def analyze_symbol(symbol):
    now = int(time.time())
    current = get_price_at(symbol, now)
    if not current:
        return

    intervals = {
        "5s": (5, 2),
        "10s": (10, 3),
        "60s": (60, 5),
        "180s": (180, 8),
        "300s": (300, 10),
    }

    for label, (seconds, threshold) in intervals.items():
        past = get_price_at(symbol, now - seconds)
        if not past:
            continue
        change = ((current - past) / past) * 100
        if change >= threshold:
            notify_buy(symbol)
            break

# 🔁 تحليل مستمر لكل العملات
def analyzer_loop():
    while True:
        keys = r.keys("prices:*")
        symbols = [k.decode().split(":")[1] for k in keys]
        for symbol in symbols:
            try:
                analyze_symbol(symbol)
            except Exception as e:
                print(f"❌ {symbol}: {e}")
        time.sleep(COLLECT_INTERVAL)

# 📊 أفضل العملات خلال فترة معينة
def get_top_movers(minutes=5, top_n=5):
    now = int(time.time())
    keys = r.keys("prices:*")
    result = []
    for key in keys:
        symbol = key.decode().split(":")[1]
        current = get_price_at(symbol, now)
        past = get_price_at(symbol, now - minutes * 60)
        if current and past:
            change = ((current - past) / past) * 100
            result.append((symbol, round(change, 2)))
    result.sort(key=lambda x: x[1], reverse=True)
    return result[:top_n]

# 📨 أمر /السجل من تلغرام
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    text = data.get("message", {}).get("text", "")
    if "السجل" in text:
        total = len(r.keys("prices:*"))
        movers_5 = get_top_movers(5)
        movers_10 = get_top_movers(10)

        msg = f"📊 العملات المخزنة: {total}\n\n"
        msg += "🔥 أفضل 5 خلال 5 دقائق:\n"
        for sym, ch in movers_5:
            msg += f"- {sym}: {ch}%\n"
        msg += "\n⚡️ أفضل 5 خلال 10 دقائق:\n"
        for sym, ch in movers_10:
            msg += f"- {sym}: {ch}%\n"

        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            print("❌ فشل إرسال السجل:", e)

    return "OK", 200

# 🚀 تشغيل النظام
if __name__ == "__main__":
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=analyzer_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)