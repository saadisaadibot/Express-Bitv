# -*- coding: utf-8 -*-
import os, time, math, json, requests, threading
from datetime import datetime, timezone
from flask import Flask, request

# ======= إعدادات قابلة للتعديل =======
TOP_N                  = int(os.getenv("TOP_N", 10))           # حجم الغرفة
REFRESH_LOW_EVERY_SEC  = int(os.getenv("REFRESH_LOW_EVERY_SEC", 300))  # كل كم ثانية نعيد قيعان 12h
SCAN_INTERVAL_SEC      = int(os.getenv("SCAN_INTERVAL_SEC", 15))       # تحديث الأسعار والترتيب
MIN_PRICE_EUR          = float(os.getenv("MIN_PRICE_EUR", 0.0005))
DROP_KICK_PCT          = float(os.getenv("DROP_KICK_PCT", -0.8))       # إخراج عند تراجع قصير
SHORT_WINDOW_MIN       = int(os.getenv("SHORT_WINDOW_MIN", 2))         # نافذة التراجع القصير
MAX_MARKETS_PER_TICK   = int(os.getenv("MAX_MARKETS_PER_TICK", 120))   # حد قراءة الأسعار

# فلترة “الترند النظيف” داخل السجل
UP_CH1H_MIN      = float(os.getenv("UP_CH1H_MIN", 0.5))  # آخر ساعة ≥ +0.5%
HL_MIN_GAP_PCT   = float(os.getenv("HL_MIN_GAP_PCT", 0.3))   # HL gap
MAX_RED_CANDLE   = float(os.getenv("MAX_RED_CANDLE", -2.0))  # ممنوع شمعة <= -2% بآخر ساعة
SWING_DEPTH      = int(os.getenv("SWING_DEPTH", 3))          # حساسية القيعان/القمم
MIN_ABOVE_L2_PCT = float(os.getenv("MIN_ABOVE_L2_PCT", 0.5)) # فوق L2

# تلغرام
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

# Redis (اختياري) — لمسح كل شيء عند التشغيل
r = None
try:
    import redis
    if os.getenv("REDIS_URL"):
        r = redis.from_url(os.getenv("REDIS_URL"))
        r.flushdb()  # ← مسح كامل
        print("Redis: database flushed at startup.")
except Exception as e:
    print("Redis not used or flush failed:", e)

# Bitvavo API
BV = "https://api.bitvavo.com/v2"

# ======= حالة داخلية =======
lock = threading.Lock()
markets_eur = []                        # ["ADA-EUR", ...]
low12h      = {}                        # market -> أدنى سعر 12h
last_prices = {}                        # market -> آخر سعر
short_buf   = {}                        # market -> [(ts, price), ...] نافذة قصيرة
room        = []                        # قائمة التوب 10 الحالية
in_room_set = set()
rank_table  = {}                        # market -> {"pct_from_low":x, "price":p, "low":l}

# ======= أدوات عامة =======
def now_ts():
    return int(datetime.now(timezone.utc).timestamp())

def send_msg(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# ======= Bitvavo =======
def get_markets_eur():
    r = requests.get(f"{BV}/markets", timeout=20)
    r.raise_for_status()
    all_markets = []
    for m in r.json():
        if m.get("status") == "trading" and m.get("quote") == "EUR":
            market = m["market"]
            # تحقق من دعم الشموع
            try:
                test = requests.get(f"{BV}/candles", params={"market": market, "interval": "1m", "limit": 1}, timeout=10)
                if test.status_code == 200:
                    all_markets.append(market)
            except:
                pass
    return all_markets

def get_candles_1m(market, start_ms=None, end_ms=None, limit=1200):
    params = {"market": market, "interval": "1m", "limit": limit}
    if start_ms: params["start"] = str(start_ms)
    if end_ms:   params["end"]   = str(end_ms)
    r = requests.get(f"{BV}/candles", params=params, timeout=20)
    r.raise_for_status()
    return r.json()   # [time, open, high, low, close, volume]

def get_price(market):
    r = requests.get(f"{BV}/ticker/price", params={"market": market}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])

# ======= بناء قيعان 12 ساعة =======
def rebuild_12h_lows():
    global low12h
    end = int(datetime.now(timezone.utc).timestamp()*1000)
    start = end - 12*60*60*1000
    new_lows = {}
    for i, m in enumerate(markets_eur):
        try:
            cs = get_candles_1m(m, start_ms=start, end_ms=end, limit=800)
            if not cs: 
                continue
            lows = [float(c[3]) for c in cs]
            closes = [float(c[4]) for c in cs]
            last = closes[-1]
            if last < MIN_PRICE_EUR: 
                continue
            new_lows[m] = min(lows)
            with lock:
                last_prices[m] = last
                short_buf.setdefault(m, [])
                short_buf[m].append((now_ts(), last))
                _cut_short(m)
        except Exception as e:
            print("low12h err", m, e)
        time.sleep(0.03 if (i % 40) else 0.4)
    with lock:
        low12h = new_lows

# ======= نافذة تراجع قصير =======
def _cut_short(market):
    horizon = SHORT_WINDOW_MIN * 60
    tnow = now_ts()
    buf = short_buf.get(market, [])
    short_buf[market] = [(t,p) for (t,p) in buf if tnow - t <= horizon]

# ======= تحديث الأسعار الحية بخفة =======
def update_live_prices():
    idx = 0
    while True:
        try:
            batch = markets_eur[idx: idx + MAX_MARKETS_PER_TICK]
            if not batch:
                idx = 0
                batch = markets_eur[:MAX_MARKETS_PER_TICK]
            for m in batch:
                try:
                    p = get_price(m)
                    with lock:
                        last_prices[m] = p
                        short_buf.setdefault(m, [])
                        short_buf[m].append((now_ts(), p))
                        _cut_short(m)
                except Exception as e:
                    print("price err", m, e)
                time.sleep(0.025)
            idx += MAX_MARKETS_PER_TICK
        except Exception as e:
            print("update_live_prices loop err:", e)
        time.sleep(SCAN_INTERVAL_SEC)

# ======= ترتيب “الأبعد عن القاع” =======
def compute_rank_table():
    table = []
    with lock:
        lows = dict(low12h)
        prices = dict(last_prices)
    for m, l in lows.items():
        p = prices.get(m)
        if not p or p < MIN_PRICE_EUR or l <= 0: 
            continue
        pct_from_low = (p - l) / l * 100.0
        table.append((m, pct_from_low, p, l))
    table.sort(key=lambda x: x[1], reverse=True)
    return table

# ======= إخراج بالهبوط القصير =======
def apply_drop_kick():
    global room, in_room_set
    kicked = []
    with lock:
        cur = list(room)
    for m in cur:
        buf = short_buf.get(m, [])
        if len(buf) < 2: 
            continue
        p_now = buf[-1][1]
        p_old = buf[0][1]
        if p_old <= 0: 
            continue
        ch = (p_now - p_old)/p_old*100.0
        if ch <= DROP_KICK_PCT:
            kicked.append((m, ch))
    if kicked:
        with lock:
            for m, _ in kicked:
                in_room_set.discard(m)
                if m in room: room.remove(m)
        send_msg("⬇️ خروج بالتراجع القصير: " + ", ".join([k[0].split("-")[0] for k in kicked]))

# ======= تحديث الغرفة من جدول الترتيب =======
def rebuild_room_from_rank():
    global room, in_room_set, rank_table
    table = compute_rank_table()
    new_room = [m for (m, pct, p, l) in table[:TOP_N]]
    new_set  = set(new_room)
    with lock:
        removed = [m for m in room if m not in new_set]
        added   = [m for m in new_room if m not in in_room_set]
        room = new_room
        in_room_set = new_set
        rank_table = {m: {"pct_from_low": pct, "price": p, "low": l}
                      for (m, pct, p, l) in table[:TOP_N]}
    if removed:
        send_msg("↘️ خرجت: " + ", ".join([x.split("-")[0] for x in removed]))
    if added:
        send_msg("↗️ دخلت: " + ", ".join([x.split("-")[0] for x in added]))

# ======= كاشف القاعين HL + بدون كسر + صعود ساعة =======
def _is_local_min(closes, i, depth):
    left  = all(closes[i] <= closes[k] for k in range(max(0, i-depth), i))
    right = all(closes[i] <= closes[k] for k in range(i+1, min(len(closes), i+1+depth)))
    return left and right

def _last_two_swings_low(closes, depth):
    lows = []
    for i in range(depth, len(closes)-depth):
        if _is_local_min(closes, i, depth):
            lows.append((i, closes[i]))
    return lows[-2:] if len(lows) >= 2 else None

def is_strong_clean_uptrend(market):
    try:
        cs = get_candles_1m(market, limit=90)        # ~90 دقيقة
        if len(cs) < 30:
            return False
        closes = [float(c[4]) for c in cs]
        p_now  = closes[-1]

        # 1) آخر ساعة صاعدة
        base = closes[-61] if len(closes) >= 62 else closes[0]
        ch1h = (p_now - base) / base * 100.0
        if ch1h < UP_CH1H_MIN: 
            return False

        # 2) لا شمعة <= -2% في آخر ساعة
        start_idx = max(1, len(closes)-61)
        for i in range(start_idx, len(closes)):
            step = (closes[i] - closes[i-1]) / closes[i-1] * 100.0
            if step <= MAX_RED_CANDLE:
                return False

        # 3) HL: قاع ثاني أعلى من الأول
        swings = _last_two_swings_low(closes, SWING_DEPTH)
        if not swings: 
            return False
        (i1, L1), (i2, L2) = swings
        if (L2 - L1) / L1 * 100.0 < HL_MIN_GAP_PCT:
            return False

        # 4) بدون كسر L2 بعد تكوّنه
        if any(p < L2 for p in closes[i2:]):
            return False

        # 5) السعر الحالي فوق L2 بهامش
        if (p_now - L2) / L2 * 100.0 < MIN_ABOVE_L2_PCT:
            return False

        return True
    except Exception:
        return False

# ======= نص السجل =======
def snapshot_text():
    lines = []
    with lock:
        items = [(m, rank_table.get(m, {}).get("pct_from_low", 0.0),
                  rank_table.get(m, {}).get("price", 0.0)) for m in room]
    rank = 1
    for (m, pct_from_low, p) in items:
        if not is_strong_clean_uptrend(m):
            continue
        coin = m.split("-")[0]
        lines.append(f"{rank:02d}. ✅ {coin}  +{pct_from_low:.2f}% من قاع 12h | الآن {p:.6f}€")
        rank += 1
        if rank > TOP_N:
            break
    if not lines:
        return "⚠️ لا توجد حالياً عملات مطابقة لشرط: HL + بدون كسر + صعود آخر ساعة."
    return "📊 توب 10 (ترند نظيف دون كسر آخر ساعة):\n" + "\n".join(lines)

# ======= اللوبات =======
def lows_refresh_loop():
    while True:
        try:
            rebuild_12h_lows()
            rebuild_room_from_rank()
        except Exception as e:
            print("lows_refresh_loop err:", e)
        time.sleep(REFRESH_LOW_EVERY_SEC)

def ranking_loop():
    while True:
        try:
            apply_drop_kick()
            rebuild_room_from_rank()
        except Exception as e:
            print("ranking_loop err:", e)
        time.sleep(SCAN_INTERVAL_SEC)

def main_threads():
    threading.Thread(target=update_live_prices, daemon=True).start()
    threading.Thread(target=lows_refresh_loop, daemon=True).start()
    threading.Thread(target=ranking_loop, daemon=True).start()

# ======= Webhook بسيط (/السجل) =======
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    text = (data.get("message", {}).get("text") or "").strip()
    if text in ("/السجل", "/log"):
        send_msg(snapshot_text())
    elif text in ("/start", "ابدأ"):
        send_msg("✅ الصيّاد يعمل. أرسل /السجل لعرض التوب 10.")
    return "ok"

def run_web():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

# ======= تشغيل =======
if __name__ == "__main__":
    send_msg("✅ الصيّاد بدأ: Top10 الأبعد عن قاع 12h (+ فلترة HL/بدون كسر/صعود 1h).")
    markets_eur = get_markets_eur()
    rebuild_12h_lows()
    rebuild_room_from_rank()
    main_threads()
    # شغّل الويبهوك في خيط (أو شغّل بـ gunicorn في الإنتاج)
    threading.Thread(target=run_web, daemon=True).start()
    while True:
        time.sleep(60)