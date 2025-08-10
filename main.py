# -*- coding: utf-8 -*-
import os, time, math, json, requests, threading
from datetime import datetime, timedelta, timezone
from collections import deque, defaultdict

# ========= إعدادات قابلة للتعديل =========
TOP_N                = int(os.getenv("TOP_N", 10))
REFRESH_DAILY_HHMM   = os.getenv("REFRESH_DAILY_HHMM", "00:05")   # توقيت بناء قائمة اليوم
SCAN_INTERVAL_SEC    = int(os.getenv("SCAN_INTERVAL_SEC", 60))    # مراقبة كل دقيقة
LOOKBACK_24H_MIN     = 24*60
LOOKBACK_3H_MIN      = 180
SLOPE_WINDOW_MIN     = 30
VOL_SPIKE_WINDOW_MIN = 30        # مقارنة حجم آخر 30 دقيقة بمتوسط اليوم
DROP_FROM_PEAK_PCT   = float(os.getenv("DROP_FROM_PEAK_PCT", -2.0))  # خروج إذا هبطت من قمتها اليومية بهذه النسبة
MIN_PRICE_EUR        = float(os.getenv("MIN_PRICE_EUR", 0.0005))     # تجاهل القيعان الميّتة جداً

# ========= مفاتيح تلغرام =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

# ========= Bitvavo API =========
BV = "https://api.bitvavo.com/v2"

# ========= حالة اليوم =========
room = []                     # [ "COIN-EUR", ... ] العملات المختارة لليوم
scores_today = {}             # "COIN-EUR" -> score
peaks_today  = {}             # "COIN-EUR" -> أعلى سعر تحقق اليوم
watch_pool   = []             # مرشحين احتياطيين مرتبين حسب السكور
lock = threading.Lock()

# ========= أدوات مساعدة =========
def now_utc():
    return datetime.now(timezone.utc)

def send_message(text):
    if not BOT_TOKEN or not CHAT_ID: 
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("Telegram error:", e)

def get_markets_eur():
    # يرجع كل الأسواق /EUR المفعّلة
    r = requests.get(f"{BV}/markets")
    r.raise_for_status()
    out = []
    for m in r.json():
        if m.get("status") == "trading" and m.get("quote") == "EUR":
            out.append(m["market"])
    return out

def get_candles(market, interval="1m", start_ms=None, end_ms=None, limit=1200):
    params = {"market": market, "interval": interval, "limit": limit}
    if start_ms: params["start"] = str(start_ms)
    if end_ms:   params["end"]   = str(end_ms)
    r = requests.get(f"{BV}/candles", params=params, timeout=15)
    r.raise_for_status()
    # كل عنصر: [time, open, high, low, close, volume]
    return r.json()

def pct(a, b):
    try:
        if b == 0: return 0.0
        return (a - b) / b * 100.0
    except: 
        return 0.0

def avg(x): 
    return sum(x)/len(x) if x else 0.0

def last_close(candles):
    return float(candles[-1][4]) if candles else 0.0

def series_from_candles(candles):
    closes = [float(c[4]) for c in candles]
    vols   = [float(c[5]) for c in candles]
    return closes, vols

def slope_pct(closes):
    # ميل تقريبـي خلال النافذة: (C_last - C_first) / C_first
    if len(closes) < 2 or closes[0] <= 0: return 0.0
    return (closes[-1] - closes[0]) / closes[0] * 100.0

def build_score(market):
    # نجلب شموع 1m لـ 24h (حدود 1440 شمعة). نقسم القراءة لأجزاء مختصرة لتقليل الضغط.
    end   = int(now_utc().timestamp()*1000)
    start = end - LOOKBACK_24H_MIN*60*1000
    candles = get_candles(market, "1m", start, end, limit=min(1440, LOOKBACK_24H_MIN+5))
    if len(candles) < 60: 
        return None

    closes, vols = series_from_candles(candles)
    price        = closes[-1]
    if price < MIN_PRICE_EUR: 
        return None

    # Δ24h%
    ch24 = pct(closes[-1], closes[0])

    # Δ3h%
    k3   = min(LOOKBACK_3H_MIN, len(closes)-1)
    ch3h = pct(closes[-1], closes[-1-k3])

    # Slope 30m
    k30  = min(SLOPE_WINDOW_MIN, len(closes)-1)
    sl30 = slope_pct(closes[-1-k30:])

    # Vol spike: متوسط آخر 30 دقيقة مقابل متوسط اليوم
    v30  = avg(vols[-min(VOL_SPIKE_WINDOW_MIN, len(vols)):])
    vday = avg(vols)
    vol_spike = (v30 / vday) if (vday and vday > 0) else 1.0
    vol_component = (vol_spike-1.0)*100.0  # يحوّل المضاعِف لنقاط %

    score = ch24 + 0.7*ch3h + 0.5*sl30 + 0.3*vol_component
    return {
        "market": market,
        "price": price,
        "ch24": ch24,
        "ch3h": ch3h,
        "sl30": sl30,
        "volx": vol_spike,
        "score": score
    }

def daily_top_list():
    markets = get_markets_eur()
    # ممكن يكون كتير—نخفف الضغط: نقي الفلاتر الأولية بسرعة بـ 5m تغيّر
    top = []
    for m in markets:
        try:
            # لقطة سريعة 5m (6 شموع 1m فقط) لتصفية السكون
            snap = get_candles(m, "1m", limit=6)
            if len(snap) < 6: 
                continue
            c = [float(x[4]) for x in snap]
            if pct(c[-1], c[0]) < -5.0:    # هبوط قوي—تجاهل مبدأياً
                continue
            info = build_score(m)
            if info:
                top.append(info)
            time.sleep(0.03)  # تهدئة بسيطة للـ API
        except Exception as e:
            print("score err", m, e)
            time.sleep(0.05)
    top.sort(key=lambda x: x["score"], reverse=True)
    return top

def reset_daily_room():
    global room, scores_today, peaks_today, watch_pool
    send_message("🔄 بدء مسح يومي… بناء Top10 على حركة السعر فقط.")
    top = daily_top_list()
    with lock:
        watch_pool = [t for t in top]  # احتفظ بكل شيء كاحتياطي
        room = [t["market"] for t in top[:TOP_N]]
        scores_today = {t["market"]: t for t in top[:TOP_N]}
        peaks_today  = {m: scores_today[m]["price"] for m in room}
    names = ", ".join([m.split("-")[0] for m in room])
    send_message(f"🎯 قائمة اليوم (Top{TOP_N}): {names}")

def ensure_daily_refresh_thread():
    def worker():
        last_day = None
        while True:
            hhmm = now_utc().strftime("%H:%M")
            day  = now_utc().date()
            if (last_day != day and hhmm >= REFRESH_DAILY_HHMM) or not room:
                try:
                    reset_daily_room()
                    last_day = day
                except Exception as e:
                    send_message(f"⚠️ فشل تحديث اليوم: {e}")
            time.sleep(20)
    threading.Thread(target=worker, daemon=True).start()

def current_price(market):
    r = requests.get(f"{BV}/ticker/price", params={"market": market}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])

def replace_weak_if_needed():
    # إذا عملة في الغرفة هبطت -2% من قمتها اليومية → استبدلها بأقوى مرشح غير موجود
    global room, watch_pool, peaks_today, scores_today
    if not room: return

    outlist = []
    with lock:
        for m in list(room):
            try:
                p = current_price(m)
                pk = peaks_today.get(m, p)
                if p > pk: 
                    peaks_today[m] = p
                drop = pct(p, pk)
                if drop <= DROP_FROM_PEAK_PCT:
                    outlist.append((m, drop))
            except Exception as e:
                print("price err", m, e)

        # استبدالات
        for (weak, drop) in outlist:
            # اختر أول مرشح من الاحتياطي غير موجود في الغرفة
            repl = None
            for t in watch_pool:
                if t["market"] not in room:
                    repl = t; break
            if not repl: 
                # إن لم يوجد، أعد بناء احتياطي مختصر
                candidates = daily_top_list()
                repl = next((t for t in candidates if t["market"] not in room), None)

            if repl:
                room.remove(weak)
                room.append(repl["market"])
                scores_today.pop(weak, None)
                scores_today[repl["market"]] = repl
                peaks_today.pop(weak, None)
                peaks_today[repl["market"]] = repl["price"]
                send_message(f"♻️ استبدال: خرجت {weak.split('-')[0]} (هبوط {abs(drop):.2f}%) ← دخلت {repl['market'].split('-')[0]}")
            else:
                # لا بديل متاح
                room.remove(weak)
                scores_today.pop(weak, None)
                peaks_today.pop(weak, None)
                send_message(f"⬇️ خروج: {weak.split('-')[0]} (هبوط {abs(drop):.2f}%).")

def monitor_loop():
    while True:
        try:
            if not room:
                time.sleep(5); 
                continue
            with lock:
                markets = list(room)
            # تحديث قمم وإشعار عند قفزات ضمن الغرفة فقط
            for m in markets:
                try:
                    p = current_price(m)
                    pk = peaks_today.get(m, p)
                    if p > pk:
                        peaks_today[m] = p
                        # قفزة 1% فوق آخر قمة → إشارة “قوة”
                        if pct(p, pk) > 1.0:
                            send_message(f"🚀 قوة مستمرة داخل الغرفة: {m.split('-')[0]} ارتفع فوق قمته اليومية.")
                except Exception as e:
                    print("monitor price err", m, e)
                    time.sleep(0.05)
            replace_weak_if_needed()
        except Exception as e:
            print("monitor loop err:", e)
        time.sleep(SCAN_INTERVAL_SEC)

def main():
    ensure_daily_refresh_thread()
    monitor_loop()

if __name__ == "__main__":
    send_message("✅ الصيّاد بدأ العمل (Top10 يومي — حركة سعر فقط).")
    main()