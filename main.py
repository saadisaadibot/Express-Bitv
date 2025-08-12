# -*- coding: utf-8 -*-
"""
Sniper (REST-only) — تحمية ⇢ انفجار
- بدون Redis
- ضغط منخفض على Bitvavo
- أمر تلغرام واحد: /الحالة
"""

import os, time, math, json, threading, random
from collections import deque
from datetime import datetime
import requests
from flask import Flask, request, jsonify

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
DISCOVERY_SEC       = int(os.getenv("DISCOVERY_SEC", 120))   # كل كم ثانية نحدّث المرشحين
ROOM_CAP            = int(os.getenv("ROOM_CAP", 24))         # سعة غرفة العمليات
TTL_MIN             = int(os.getenv("TTL_MIN", 30))          # مدة بقاء الرمز (دقائق)
TICK_SEC            = float(os.getenv("TICK_SEC", 3.0))      # دورة المراقبة (ثوانٍ)
EXCLUDE_24H_PCT     = float(os.getenv("EXCLUDE_24H_PCT", 12.0))  # استثناء رابحين 24h الكبار

# عتبات التحمية
PRE_R60             = float(os.getenv("PRE_R60", 0.40))      # %
PRE_R20             = float(os.getenv("PRE_R20", 0.20))      # %
PRE_NODIP           = float(os.getenv("PRE_NODIP", 0.25))    # %
PRE_VOLBOOST        = float(os.getenv("PRE_VOLBOOST", 1.4))  # ×

# عتبات التريغر (الانفجار)
TRIG_R40            = float(os.getenv("TRIG_R40", 0.80))     # %
TRIG_R120           = float(os.getenv("TRIG_R120", 1.60))    # %
TRIG_R20HELP        = float(os.getenv("TRIG_R20HELP", 0.30)) # %
TRIG_VOLZ           = float(os.getenv("TRIG_VOLZ", 1.2))     # Z-score

ALERT_COOLDOWN_SEC  = int(os.getenv("ALERT_COOLDOWN_SEC", 180))
SPREAD_MAX_BP       = int(os.getenv("SPREAD_MAX_BP", 30))    # 0.30% أقصى سبريد لحظة الإشارة

# تلغرام/إشعارات (اختياري)
BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
CHAT_ID             = os.getenv("CHAT_ID", "")
ENABLE_ALERTS       = int(os.getenv("ENABLE_ALERTS", "0"))   # 0 افتراضيًا
SAQR_WEBHOOK        = os.getenv("SAQR_WEBHOOK", "")          # اختياري "https://.../webhook"

# Bitvavo
BASE_URL            = os.getenv("BITVAVO_URL", "https://api.bitvavo.com")
TIMEOUT             = float(os.getenv("HTTP_TIMEOUT", 8.0))

# =========================
# 🌐 HTTP Session + Retry بسيط
# =========================
session = requests.Session()
session.headers.update({"User-Agent":"Nems-Sniper/1.1"})
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_connections=50, pool_maxsize=50)
session.mount("https://", adapter)
session.mount("http://", adapter)

def http_get(path, params=None):
    url = f"{BASE_URL}{path}"
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 429:
            time.sleep(0.6 + random.random() * 0.6)  # Backoff بسيط
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[HTTP] GET {path} failed: {e}")
        return None

# =========================
# 🧰 أدوات مساعدة
# =========================
def pct(a, b):
    if b is None or b == 0:
        return 0.0
    return (a - b) / b * 100.0

def now_ts():
    return time.time()

def zscore(x, mu, sigma):
    if sigma <= 1e-12:
        return 0.0
    return (x - mu) / sigma

def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

# =========================
# ✅ أسواق مدعومة (حل مشكلة 404)
# =========================
SUPPORTED_MARKETS = set()

def load_supported_markets():
    """نجلب كل الماركتات مرة واحدة ونبني set لأزواج EUR فقط."""
    global SUPPORTED_MARKETS
    SUPPORTED_MARKETS.clear()
    data = http_get("/v2/markets")
    if not data:
        print("[MARKETS] فشل جلب /v2/markets — سنحاول لاحقًا")
        return
    for m in data:
        market = (m.get("market") or "").upper()
        if market.endswith("-EUR"):
            SUPPORTED_MARKETS.add(market)
    print(f"[MARKETS] loaded {len(SUPPORTED_MARKETS)} EUR markets")

def is_supported_market(market: str) -> bool:
    return bool(market) and market.upper() in SUPPORTED_MARKETS

# =========================
# 🧠 حالة الغرفة
# =========================
class CoinState:
    __slots__ = ("symbol","market","entered_at","expires_at","preheat",
                 "last_alert_at","buffer","vol_hist","vol_mu","vol_sigma",
                 "last_seen_price","last_seen_time","debounce_ok","promoted")

    def __init__(self, symbol, market=None):
        self.symbol = symbol.upper()
        self.market = (market or f"{self.symbol}-EUR").upper()
        self.entered_at = now_ts()
        self.expires_at = self.entered_at + TTL_MIN*60
        self.preheat = False
        self.promoted = False
        self.last_alert_at = 0
        self.buffer = deque(maxlen=600)     # ~ آخر 30 دقيقة عند 3s
        self.vol_hist = deque(maxlen=20)    # آخر 20 دقيقة (1m volumes)
        self.vol_mu = 0.0
        self.vol_sigma = 0.0
        self.last_seen_price = None
        self.last_seen_time = 0
        self.debounce_ok = 0

    def renew(self):
        self.expires_at = now_ts() + TTL_MIN*60

    def add_price(self, ts, price):
        self.last_seen_price = price
        self.last_seen_time = ts
        self.buffer.append((ts, price))

    def r_change(self, seconds):
        if not self.buffer:
            return 0.0
        t_now, p_now = self.buffer[-1]
        t_target = t_now - seconds
        base = None
        for (t, p) in reversed(self.buffer):
            if t <= t_target:
                base = p
                break
        if base is None:
            base = self.buffer[0][1]
        return pct(p_now, base)

    def drawdown_pct(self, seconds):
        if not self.buffer:
            return 0.0
        t_now, _ = self.buffer[-1]
        t_lo = t_now - seconds
        hi = -1e18
        last = None
        for (t,p) in self.buffer:
            if t >= t_lo:
                hi = max(hi, p)
                last = p
        if hi < 0 or last is None:
            return 0.0
        return pct(last, hi) * -1

    def volz(self):
        return zscore(self.vol_hist[-1] if self.vol_hist else 0.0, self.vol_mu, self.vol_sigma)

# ذاكرات عامة
room_lock = threading.Lock()
room: dict[str,CoinState] = {}     # market -> state
watchlist: set[str] = set()
last_discovery_at = 0
backoff_mode = False

# =========================
# 🔔 تلغرام / إشعارات
# =========================
def tg_send(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=8)
    except Exception as e:
        print("[TG] send failed:", e)

def notify_explosion(symbol, rank_hint=""):
    if not ENABLE_ALERTS:
        return
    msg = f"🚀 {symbol} جاهزة (تحمية⇢انفجار){'  #'+rank_hint if rank_hint else ''}"
    tg_send(msg)
    if SAQR_WEBHOOK:
        try:
            session.post(SAQR_WEBHOOK, json={"text": f"اشتري {symbol}"}, timeout=6)
        except Exception as e:
            print("[SAQR] post failed:", e)

# =========================
# 🔎 الاستكشاف Top5m (كل 120ث)
# =========================
def read_ticker_24h():
    data = http_get("/v2/ticker/24h")
    if not data:
        return []
    out = []
    for it in data:
        market = (it.get("market","") or "").upper()
        if not market.endswith("-EUR"):
            continue
        last = float(it.get("last", it.get("lastPrice", 0.0)) or 0.0)
        openp = float(it.get("open", 0.0) or 0.0)
        vol   = float(it.get("volume", 0.0) or 0.0)  # base volume
        pct24 = float(it.get("priceChangePercentage", 0.0) or 0.0)
        bid   = float(it.get("bid", 0.0) or 0.0)
        ask   = float(it.get("ask", 0.0) or 0.0)
        spread_bp = 0.0
        if bid and ask:
            spread_bp = (ask - bid) / ((ask+bid)/2) * 10000
        out.append({
            "market": market, "symbol": market.split("-")[0],
            "last": last, "open": openp, "volume": vol, "pct24": pct24,
            "bid": bid, "ask": ask, "spread_bp": spread_bp
        })
    return out

def read_last_candles_1m(market, limit=10):
    """
    يحاول مسارين:
    1) /v2/candles?market=MARKET&interval=1m&limit=N
    2) /v2/MARKET/candles?interval=1m&limit=N (fallback)
    """
    if not market:
        return []
    market = market.upper().strip()
    if not is_supported_market(market):
        return []
    params = {"market": market, "interval":"1m", "limit": int(limit)}
    data = http_get("/v2/candles", params=params)
    if isinstance(data, list):
        return data
    alt_path = f"/v2/{market}/candles"
    data2 = http_get(alt_path, params={"interval":"1m", "limit": int(limit)})
    if isinstance(data2, list):
        return data2
    return []

def compute_5m_change_from_candles(candles):
    if len(candles) < 6:
        return 0.0
    c_now = float(candles[-1][4])
    c_5m  = float(candles[-6][4])
    return pct(c_now, c_5m)

def discovery_loop():
    global last_discovery_at
    while True:
        t0 = now_ts()
        last_discovery_at = t0
        try:
            # حمّل قائمة الأسواق مرة أولى وثم كل 30 دقيقة
            if not SUPPORTED_MARKETS or (int(t0) % (30*60) < 2):
                load_supported_markets()

            tick = read_ticker_24h()
            if not tick:
                time.sleep(5); continue

            # استثناء رابحين 24h الكبار + حصر على المدعوم فعلًا
            tick = [
                x for x in tick
                if x["pct24"] < EXCLUDE_24H_PCT and is_supported_market(x["market"])
            ]

            # تقدير سيولة باليورو ~ volume_base * last
            for x in tick:
                x["eur_volume"] = x["volume"] * (x["last"] or 0.0)

            # أعلى 60 سيولة
            tick.sort(key=lambda x: x["eur_volume"], reverse=True)
            top = tick[:60]

            # احسب 5m change لهؤلاء فقط
            five_map = {}
            for batch in chunks(top, 12):
                for x in batch:
                    m = x["market"]
                    cnd = read_last_candles_1m(m, limit=10)
                    ch5 = compute_5m_change_from_candles(cnd)
                    five_map[m] = ch5
                time.sleep(0.35)

            sorted_top = sorted(
                (x for x in top if x["market"] in five_map),
                key=lambda x: five_map.get(x["market"], 0.0),
                reverse=True
            )
            pick = sorted_top[:max(ROOM_CAP, 20)]

            # حدّث الغرفة
            with room_lock:
                wanted = {p["market"] for p in pick}
                # جدّد TTL لمن هم داخل & بقائمة توب5م
                for m, st in list(room.items()):
                    if m in wanted:
                        st.renew()
                # أدخل الجدد
                for p in pick:
                    m = p["market"]
                    sym = p["symbol"]
                    if m not in room and is_supported_market(m):
                        st = CoinState(sym, m)
                        room[m] = st
                        watchlist.add(m)
                # قص الزائد
                if len(room) > ROOM_CAP:
                    scored = []
                    for m, st in room.items():
                        r60 = st.r_change(60) if st.buffer else -999
                        scored.append((r60, m))
                    scored.sort()
                    for _, m in scored[:len(room)-ROOM_CAP]:
                        room.pop(m, None)
                        watchlist.discard(m)
        except Exception as e:
            print("[DISCOVERY] error:", e)
        slept = now_ts() - t0
        time.sleep(max(2.0, DISCOVERY_SEC - slept))

# =========================
# 📈 تحديث حجم 1m الدوري للغرفة (للـ VolZ)
# =========================
def refresh_room_volume_loop():
    while True:
        try:
            with room_lock:
                markets = list(room.keys())
            for batch in chunks(markets, 12):
                for m in batch:
                    cnd = read_last_candles_1m(m, limit=5)
                    if not cnd:
                        continue
                    vol_last = float(cnd[-1][5])
                    with room_lock:
                        st = room.get(m)
                        if not st: 
                            continue
                        st.vol_hist.append(vol_last)
                        if len(st.vol_hist) >= 3:
                            mu = sum(st.vol_hist)/len(st.vol_hist)
                            var = sum((v-mu)*(v-mu) for v in st.vol_hist)/len(st.vol_hist)
                            st.vol_mu = mu
                            st.vol_sigma = math.sqrt(var)
                time.sleep(0.35)
        except Exception as e:
            print("[VOL] error:", e)
        time.sleep(60)  # مرة كل دقيقة

# =========================
# 🩺 مراقبة حيّة (REST-only) + Backoff
# =========================
def fetch_price(market):
    data = http_get("/v2/ticker/price", params={"market": market})
    if not data: 
        return None
    try:
        return float(data.get("price") or 0.0)
    except Exception:
        return None

def monitoring_loop():
    global backoff_mode
    rr_idx = 0
    while True:
        t_start = now_ts()
        try:
            with room_lock:
                markets = list(room.keys())
            if not markets:
                time.sleep(TICK_SEC); continue

            BATCH = max(8, min(12, len(markets)//2 + 1))
            slice_ = markets[rr_idx:rr_idx+BATCH]
            if not slice_:
                rr_idx = 0
                slice_ = markets[:BATCH]
            rr_idx += BATCH

            errors = 0
            for m in slice_:
                p = fetch_price(m)
                if p is None:
                    errors += 1
                    continue
                ts = now_ts()
                with room_lock:
                    st = room.get(m)
                    if not st:
                        continue
                    st.add_price(ts, p)

                    # حذف المنتهية
                    if ts >= st.expires_at:
                        room.pop(m, None)
                        watchlist.discard(m)
                        continue

                    # حسابات سريعة
                    r20  = st.r_change(20)
                    r40  = st.r_change(40)
                    r60  = st.r_change(60)
                    r120 = st.r_change(120)
                    dip20 = st.drawdown_pct(20)
                    volZ  = st.volz()

                    # -------- تحمية --------
                    vol_boost_ok = (st.vol_hist and (st.vol_hist[-1] >= PRE_VOLBOOST * (st.vol_mu or 0.0000001)))
                    if (r60 >= PRE_R60 and r20 >= PRE_R20 and dip20 <= PRE_NODIP and vol_boost_ok):
                        st.debounce_ok = min(2, st.debounce_ok+1)
                        if st.debounce_ok >= 2:
                            st.preheat = True
                    else:
                        if r60 < 0 or dip20 > (PRE_NODIP*2) or (st.vol_hist and st.vol_hist[-1] < 1.1*(st.vol_mu or 0.0000001)):
                            st.preheat = False
                        st.debounce_ok = 0

                    # -------- انفجار --------
                    if st.preheat:
                        trig_fast  = (r40 >= TRIG_R40 and dip20 <= (PRE_NODIP+0.05))
                        trig_accum = (r120 >= TRIG_R120 and r20 >= TRIG_R20HELP)
                        vol_ok     = (volZ >= TRIG_VOLZ)
                        cooldown_ok = (ts - st.last_alert_at) >= ALERT_COOLDOWN_SEC
                        if (trig_fast or trig_accum) and vol_ok and cooldown_ok:
                            st.last_alert_at = ts
                            notify_explosion(st.symbol)
            backoff_mode = (errors >= max(3, len(slice_)//3))
        except Exception as e:
            print("[MONITOR] error:", e)
            backoff_mode = True

        base = TICK_SEC if not backoff_mode else max(TICK_SEC, 5.0)
        jitter = random.uniform(0.05, 0.25)
        elapsed = now_ts() - t_start
        time.sleep(max(0.2, base + jitter - elapsed))

# =========================
# 🧾 واجهة Flask + تلغرام
# =========================
app = Flask(__name__)

@app.route("/")
def root():
    return "Sniper REST is alive ✅"

@app.route("/webhook", methods=["POST"])
def tg_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg = data.get("message", {})
        txt = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id") or "")
        if not txt:
            return jsonify(ok=True)
        cmd = txt.lower().strip()
        if cmd in ("/الحالة", "الحالة", "/status", "status"):
            return jsonify(ok=True), (send_status(chat_override=chat_id) or 200)
        else:
            return jsonify(ok=True)
    except Exception as e:
        print("[TG] webhook err:", e)
        return jsonify(ok=True)

def fmt_secs(sec):
    sec = int(max(0, sec))
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"

def send_status(chat_override=None):
    with room_lock:
        items = list(room.items())
    if not items:
        text = "📊 الحالة: لا توجد عملات في الغرفة حاليًا."
        if chat_override:
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                session.post(url, json={"chat_id": chat_override, "text": text, "disable_web_page_preview": True}, timeout=8)
            except Exception as e:
                print("[TG] send status failed:", e)
        else:
            tg_send(text)
        return

    ready, warm, normal = [], [], []
    nowt = now_ts()
    for m, st in items:
        r60  = st.r_change(60)
        r120 = st.r_change(120)
        volZ = st.volz()
        ttl  = fmt_secs(int(st.expires_at - nowt))
        if (nowt - st.last_alert_at) < 10:
            ready.append((m, st, r60, r120, volZ, ttl))
        elif st.preheat:
            warm.append((m, st, r60, r120, volZ, ttl))
        else:
            normal.append((m, st, r60, r120, volZ, ttl))

    def line(tag, tup):
        m, st, r60, r120, vz, ttl = tup
        sym = st.symbol
        return f"{tag} {sym:<7} r60={r60:+.2f}% r120={r120:+.2f}%  VolZ={vz:+.2f}  ⏳{ttl}"

    lines = []
    lines.append(f"📊 الحالة — غرفة: {len(items)}/{ROOM_CAP}  |  Backoff: {'ON' if backoff_mode else 'OFF'}")

    if ready:
        lines.append("\n🚀 جاهزة:")
        for t in sorted(ready, key=lambda x: (x[2], x[3]), reverse=True)[:10]:
            lines.append(line("•", t))
    if warm:
        lines.append("\n🔥 تحمية:")
        for t in sorted(warm, key=lambda x: (x[2], x[3]), reverse=True)[:10]:
            lines.append(line("•", t))
    if normal:
        lines.append("\n🟢 مراقبة:")
        for t in sorted(normal, key=lambda x: (x[2], x[3]), reverse=True)[:10]:
            lines.append(line("•", t))

    text = "\n".join(lines)
    if chat_override:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            session.post(url, json={"chat_id": chat_override, "text": text, "disable_web_page_preview": True}, timeout=8)
        except Exception as e:
            print("[TG] send status failed:", e)
    else:
        tg_send(text)

# =========================
# 🚀 تشغيل الخيوط
# =========================
def start_threads():
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=refresh_room_volume_loop, daemon=True).start()
    threading.Thread(target=monitoring_loop, daemon=True).start()

# =========================
# ▶️ الإقلاع
# =========================
# عند استخدام gunicorn: gunicorn -w 1 -b 0.0.0.0:8080 main:app
load_supported_markets()
start_threads()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))