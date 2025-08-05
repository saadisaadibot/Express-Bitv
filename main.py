import os
import time
import json
import redis
import requests
import threading
from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
IS_RUNNING_KEY = "sniper_running"
SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/webhook"

def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}
        )
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def fetch_bitvavo_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets")
        data = res.json()
        return set(m["market"].replace("-EUR", "").upper() for m in data if m["market"].endswith("-EUR"))
    except Exception as e:
        print("❌ فشل في جلب الرموز:", e)
        return set()

def get_change(symbol, minutes):
    try:
        pair = f"{symbol}-EUR"
        url = f"https://api.bitvavo.com/v2/candles/{pair}/1m?limit={minutes+1}"
        res = requests.get(url, timeout=3)

        # ✅ تحقق أن الرد غير فارغ وأنه بصيغة JSON صالحة
        if not res.content or res.status_code != 200:
            print(f"⛔ {symbol}: رد غير صالح من Bitvavo (status={res.status_code})")
            return None

        try:
            data = res.json()
        except Exception as e:
            print(f"⛔ {symbol}: فشل تحويل JSON:", e)
            return None

        if not isinstance(data, list) or len(data) < minutes + 1:
            print(f"⛔ {symbol}: عدد الشموع غير كافِ ({len(data)})")
            return None

        open_price = float(data[0][1])
        close_price = float(data[-1][4])
        change = ((close_price - open_price) / open_price) * 100
        print(f"📈 {symbol}: تغيير {change:.2f}% خلال {minutes} دقيقة")
        return round(change, 2)

    except Exception as e:
        print(f"❌ خطأ أثناء تحليل {symbol}: {e}")
        return None

def detect_explosions():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        symbols = r.hkeys("watchlist")
        for b in symbols:
            coin = b.decode()
            try:
                ch1 = get_change(coin, 1)
                ch2 = get_change(coin, 2)
                ch3 = get_change(coin, 3)

                if ch1 and ch1 >= 2:
                    notify_buy(coin, f"{ch1}% / 1m")
                elif ch2 and ch2 >= 3:
                    notify_buy(coin, f"{ch2}% / 2m")
                elif ch3 and ch3 >= 4:
                    notify_buy(coin, f"{ch3}% / 3m")

            except Exception as e:
                print(f"❌ فشل تحليل {coin}:", e)

        time.sleep(60)

def fetch_top_bitvavo():
    symbols = ["BTC", "ETH", "ADA", "XRP", "LINK", "DOGE"]
    changes = {}

    def fetch_change(symbol, interval):
        try:
            pair = f"{symbol}-EUR"
            url = f"https://api.bitvavo.com/v2/candles/{pair}/{interval}?limit=2"
            res = requests.get(url, timeout=3)

            if not res.content or res.status_code != 200:
                print(f"⛔ {symbol}: لا يوجد بيانات")
                return None

            data = res.json()
            if not isinstance(data, list) or len(data) < 2:
                print(f"⛔ {symbol}: شموع غير كافية")
                return None

            open_price = float(data[-2][1])
            close_price = float(data[-2][4])
            change = ((close_price - open_price) / open_price) * 100
            print(f"✅ {symbol}: تغيير {change:.2f}%")
            return (symbol, change)

        except Exception as e:
            print(f"❌ {symbol}: فشل التحليل:", e)
            return None

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = ex.map(lambda s: fetch_change(s, "5m"), symbols)
        for res in results:
            if res:
                changes[res[0]] = res[1]

    return sorted(changes.keys(), key=lambda x: changes[x], reverse=True)

    def collect(interval, count):
        local = []
        with ThreadPoolExecutor(max_workers=15) as ex:
            results = ex.map(lambda s: fetch_change(s, interval), symbols)
            for res in results:
                if res:
                    local.append(res)

        top = sorted(local, key=lambda x: x[1], reverse=True)[:count]
        for sym, chg in top:
            changes[sym] = chg

    collect("15m", 10)
    collect("10m", 10)
    collect("5m", 10)

    return sorted(changes.keys(), key=lambda x: changes[x], reverse=True)

def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        top_symbols = fetch_top_bitvavo()
        now = time.time()
        count = 0

        for sym in top_symbols:
            if not r.hexists("watchlist", sym):
                r.hset("watchlist", sym, now)
                count += 1

        if count == 0:
            send_message("🚫 لم يتم العثور على عملات قوية الآن.")
        else:
            print(f"✅ تمت إضافة {count} عملة للمراقبة.")

        cleanup_old_coins()
        time.sleep(180)

def cleanup_old_coins():
    now = time.time()
    for sym, ts in r.hgetall("watchlist").items():
        try:
            t = float(ts.decode())
            if now - t > 2400:
                r.hdel("watchlist", sym.decode())
        except:
            continue

def notify_buy(coin, tag):
    key = f"buy_alert:{coin}:{tag}"
    last_time = r.get(key)

    if last_time and time.time() - float(last_time) < 60:
        return

    r.set(key, time.time())
    msg = f"🚀 انفجار {tag}: {coin}"
    send_message(msg)

    try:
        payload = {"message": {"text": f"اشتري {coin}"}}
        resp = requests.post(SAQAR_WEBHOOK, json=payload)
        print(f"🛰️ إرسال إلى صقر: {payload}")
        print(f"🔁 رد صقر: {resp.status_code} - {resp.text}")
    except Exception as e:
        print("❌ فشل الإرسال إلى صقر:", e)

@app.route("/")
def home():
    return "🔥 Bitvavo Sniper is running", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify(success=True)

    text = data["message"].get("text", "").strip().lower()

    if text == "play":
        r.set(IS_RUNNING_KEY, "1")
        send_message("✅ بدأ التشغيل (Bitvavo Sniper)")

    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 تم الإيقاف المؤقت (Bitvavo Sniper)")

    elif text == "reset":
        r.delete("watchlist")
        send_message("🔄 تم مسح قائمة المراقبة.")

    elif text == "السجل":
        coins = r.hkeys("watchlist")
        if coins:
            lines = [f"{i+1}. {c.decode()}" for i, c in enumerate(coins)]
            send_message("📡 العملات المراقبة:\n" + "\n".join(lines))
        else:
            send_message("🚫 لا توجد عملات حالياً.")

    return jsonify(ok=True)

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=detect_explosions, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)