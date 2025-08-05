# ✅ السكربت كاملاً: جمع + تحليل + إشعارات + منع إشارات وهمية

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

HISTORY_SECONDS = 1800  # 30 دقيقة
FETCH_INTERVAL = 5
COOLDOWN = 60
THREAD_COUNT = 4

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
    key = f"prices:{symbol}"
    r.zadd(key, {price: now})
    r.zremrangebyscore(key, 0, cutoff)

def store_prices_threaded(prices):
    symbols = list(prices.keys())
    chunks = [symbols[i::THREAD_COUNT] for i in range(THREAD_COUNT)]
    def process(chunk):
        for sym in chunk:
            store_price(sym, prices[sym])
    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
        executor.map(process, chunks)

def get_price_at(symbol, target_time):
    key = f"prices:{symbol}"
    results = r.zrangebyscore(key, target_time - 3, target_time + 3, withscores=True)
    if results:
        return float(results[0][0])
    fallback = r.zrevrangebyscore(key, target_time - 1, 0, start=0, num=1, withscores=True)
    if fallback:
        return float(fallback[0][0])
    return None

def notify_buy(symbol):
    key = f"alerted:{symbol}"
    if r.get(key): return
    msg = f"اشتري {symbol}"
    try:
        r.set(key, "1", ex=COOLDOWN)
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": f"🚀 إشارة شراء: {msg}"})
        print(f"🚀 إشعار شراء: {msg}")
    except Exception as e:
        print(f"❌ فشل إرسال الإشعار لـ {symbol}:", e)

def analyze_symbol(symbol):
    now = int(time.time())
    current = get_price_at(symbol, now)
    if not current: return

    checks = {
        "5s": now - 5,
        "10s": now - 10,
        "60s": now - 60,
        "180s": now - 180,
        "300s": now - 300,
    }

    valid = 0
    for label, ts in checks.items():
        old = get_price_at(symbol, ts)
        if not old:
            print(f"⚠️ {symbol}: لا يوجد سعر في {label}")
            continue
        change = ((current - old) / old) * 100
        print(f"🔍 {symbol}: {label} | الآن={current:.4f}, قبل={old:.4f}, تغير={change:.2f}%")
        valid += 1
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

    # تجاهل التحليل إذا كان عندنا أقل من 3 نقاط مقارنة
    if valid < 3:
        print(f"⏳ {symbol}: بيانات غير كافية للتحليل.")

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
    top5, top10 = [], []
    for sym in symbols:
        p_now = get_price_at(sym, now)
        p_5 = get_price_at(sym, now - 300)
        p_10 = get_price_at(sym, now - 600)
        if p_now and p_5:
            top5.append((sym, round(((p_now - p_5)/p_5)*100, 2)))
        if p_now and p_10:
            top10.append((sym, round(((p_now - p_10)/p_10)*100, 2)))
    top5 = sorted(top5, key=lambda x: x[1], reverse=True)[:5]
    top10 = sorted(top10, key=lambda x: x[1], reverse=True)[:5]
    text = f"📊 العملات: {len(symbols)}\n\n📈 أعلى 5 خلال 5 دقائق:\n"
    text += "\n".join([f"- {s}: {c:.2f}%" for s,c in top5])
    text += "\n\n📈 أعلى 5 خلال 10 دقائق:\n"
    text += "\n".join([f"- {s}: {c:.2f}%" for s,c in top10])
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("❌ فشل إرسال السجل:", e)

@app.route("/")
def home():
    return "Sniper bot is alive ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if "message" not in data:
        return "no message", 200
    txt = data["message"].get("text", "").strip().lower()
    if "السجل" in txt:
        print_summary()
    return "ok", 200

def clear_old_prices():
    keys = r.keys("prices:*")
    for k in keys:
        r.delete(k)
    print("🧹 تم حذف كل الأسعار القديمة من Redis.")

# ✅ التشغيل
if __name__ == "__main__":
    clear_old_prices()
    threading.Thread(target=collector_loop, daemon=True).start()
    while not r.keys("prices:*"):
        print("⏳ بانتظار أول دفعة أسعار...")
        time.sleep(1)
    threading.Thread(target=analyzer_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))