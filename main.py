import os
import time
import redis
import requests
import threading
from dotenv import load_dotenv

load_dotenv()
r = redis.from_url(os.getenv("REDIS_URL"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/webhook"

HISTORY_SECONDS = 2 * 60 * 60  # ساعتين
FETCH_INTERVAL = 5  # كل 5 ثواني
COOLDOWN = 60  # لا تكرار إشارة خلال دقيقة

# 🟢 جلب جميع الأسعار من Bitvavo
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

# 💾 تخزين السعر في Redis مع الحذف التلقائي
def store_prices(prices):
    now = int(time.time())
    cutoff = now - HISTORY_SECONDS
    for symbol, price in prices.items():
        key = f"prices:{symbol}"
        r.zadd(key, {price: now})
        r.zremrangebyscore(key, 0, cutoff)

# 🧠 استرجاع السعر عند زمن معين
def get_price_at(symbol, target_time):
    key = f"prices:{symbol}"
    results = r.zrangebyscore(key, target_time - 2, target_time + 2, withscores=True)
    if results:
        return float(results[0][0])
    return None

# 🚀 إرسال إشارة شراء إلى صقر وإلى تلغرام
def notify_buy(symbol):
    last_key = f"alerted:{symbol}"
    if r.get(last_key):
        return
    msg = f"اشتري {symbol}"
    try:
        r.set(last_key, "1", ex=COOLDOWN)
        # إشعار إلى صقر
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
        # إشعار إلى تلغرام مباشر
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": f"🚀 إشارة شراء: {msg}"}
        )
        print(f"🚀 إشارة شراء: {msg}")
    except Exception as e:
        print(f"❌ فشل إرسال الإشارة لـ {symbol}:", e)

# 🔍 تحليل عملة واحدة
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

# 🔄 خيط التخزين المستمر
def collector_loop():
    while True:
        prices = fetch_all_prices()
        if prices:
            store_prices(prices)
            print(f"✅ تم تخزين {len(prices)} عملة.")
        time.sleep(FETCH_INTERVAL)

# 📊 أمر السجل - يطبع عدد العملات وأقوى العملات آخر 5 و10 دقائق
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

    # إرسال التقرير لتلغرام
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text}
    )

# 🚀 التشغيل
if __name__ == "__main__":
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=analyzer_loop, daemon=True).start()

    # مراقبة الأوامر من الكيبورد (محلي فقط)
    while True:
        cmd = input(">> ").strip().lower()
        if cmd == "السجل":
            print_summary()
        time.sleep(1)