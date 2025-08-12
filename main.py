# -*- coding: utf-8 -*-
import os, time, json, requests, redis
from collections import deque, defaultdict
from threading import Thread, Lock
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات قابلة للتعديل
# =========================
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", 5))
BATCH_INTERVAL_SEC   = int(os.getenv("BATCH_INTERVAL_SEC", 180))
MAX_ROOM             = int(os.getenv("MAX_ROOM", 20))
RANK_FILTER          = int(os.getenv("RANK_FILTER", 10))

BASE_STEP_PCT        = float(os.getenv("BASE_STEP_PCT", 1.0))
BASE_STRONG_SEQ      = os.getenv("BASE_STRONG_SEQ", "2,1,2")
SEQ_WINDOW_SEC       = int(os.getenv("SEQ_WINDOW_SEC", 300))
STEP_WINDOW_SEC      = int(os.getenv("STEP_WINDOW_SEC", 180))

HEAT_RET_PCT         = float(os.getenv("HEAT_RET_PCT", 0.6))
HEAT_SMOOTH          = float(os.getenv("HEAT_SMOOTH", 0.3))

BUY_COOLDOWN_SEC     = int(os.getenv("BUY_COOLDOWN_SEC", 900))
ALERT_EXPIRE_SEC     = int(os.getenv("ALERT_EXPIRE_SEC", 24*3600))
GLOBAL_WARMUP_SEC    = int(os.getenv("GLOBAL_WARMUP_SEC", 30))

LASTDAY_SKIP_PCT     = float(os.getenv("LASTDAY_SKIP_PCT", 15.0))
REVIVE_ONLY          = int(os.getenv("REVIVE_ONLY", 0))
REVIVE_CACHE_SEC     = int(os.getenv("REVIVE_CACHE_SEC", 3600))
CANDLES_LIMIT_1H     = int(os.getenv("CANDLES_LIMIT_1H", 168))

BOT_TOKEN            = os.getenv("BOT_TOKEN")
CHAT_ID              = os.getenv("CHAT_ID")
SAQAR_WEBHOOK        = os.getenv("SAQAR_WEBHOOK")
REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# =========================
# 🧠 الحالة
# =========================
r = redis.from_url(REDIS_URL)
lock = Lock()
watchlist = set()
prices = defaultdict(lambda: deque())
last_alert = {}
heat_ewma = 0.0
start_time = time.time()

# نبضات العمال (للاطمئنان)
last_beats = {"room":0, "price":0, "analyzer":0}

# =========================
# 🛰️ Bitvavo helpers
# =========================
BASE_URL = "https://api.bitvavo.com/v2"

def http_get(url, params=None, timeout=8):
    for _ in range(2):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except Exception:
            time.sleep(0.4)
    return None

def get_price(symbol):
    resp = http_get(f"{BASE_URL}/ticker/price", {"market": f"{symbol}-EUR"})
    if not resp or resp.status_code != 200: return None
    try: return float(resp.json()["price"])
    except: return None

def get_24h_change(symbol):
    ck = f"ch24:{symbol}"
    c = r.get(ck)
    if c is not None:
        try: return float(c)
        except: pass
    resp = http_get(f"{BASE_URL}/ticker/24h", {"market": f"{symbol}-EUR"})
    if not resp or resp.status_code != 200: return None
    try:
        ch = float(resp.json().get("priceChangePercentage", "0") or 0)
        r.setex(ck, 300, str(ch))
        return ch
    except: return None

def get_candles_1h(symbol, limit=CANDLES_LIMIT_1H):
    resp = http_get(f"{BASE_URL}/markets/{symbol}-EUR/candles", {"interval":"1h","limit":limit}, timeout=10)
    if not resp or resp.status_code != 200: return []
    try: return resp.json()  # [ts, open, high, low, close, vol]
    except: return []

def is_recent_exploder(symbol):
    ch24 = get_24h_change(symbol)
    return (ch24 is not None) and (ch24 >= LASTDAY_SKIP_PCT)

def is_reviving(symbol):
    key = f"revive:{symbol}"
    cached = r.get(key)
    if cached is not None:
        return cached.decode() == "1"
    candles = get_candles_1h(symbol)
    if len(candles) < 24:
        r.setex(key, REVIVE_CACHE_SEC, "0"); return False
    closes = [float(c[4]) for c in candles if len(c) >= 5]
    if len(closes) < 24:
        r.setex(key, REVIVE_CACHE_SEC, "0"); return False
    base = closes[0]; max_up = 0.0
    for c in closes[1:]:
        if base > 0:
            ch = (c - base) / base * 100.0
            if ch > max_up: max_up = ch
    ch24 = get_24h_change(symbol) or 0.0
    ok = (max_up <= 15.0) and (ch24 < 8.0)
    r.setex(key, REVIVE_CACHE_SEC, "1" if ok else "0")
    return ok

def get_5m_top_symbols(limit=MAX_ROOM):
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200: return []
    symbols = []
    try:
        for m in resp.json():
            if m.get("quote") == "EUR" and m.get("status") == "trading":
                base = m.get("base")
                if base and base.isalpha() and len(base) <= 6:
                    symbols.append(base)
    except: pass
    now = time.time(); changes = []
    for base in symbols:
        dq = prices[base]; old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270: old = pr; break
        cur = get_price(base)
        if cur is None: continue
        ch = (cur - old) / old * 100.0 if old else 0.0
        changes.append((base, ch))
        dq.append((now, cur))
        cutoff = now - 900
        while dq and dq[0][0] < cutoff: dq.popleft()
    changes.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in changes[:limit]]

def get_rank_from_bitvavo(coin):
    now = time.time(); scores = []
    with lock: wl = list(watchlist)
    for c in wl:
        dq = prices[c]; old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270: old = pr; break
        cur = prices[c][-1][1] if dq else get_price(c)
        if cur is None: continue
        ch = (cur - old) / old * 100.0 if old else 0.0
        scores.append((c, ch))
    scores.sort(key=lambda x: x[1], reverse=True)
    return {sym:i+1 for i,(sym,_) in enumerate(scores)}.get(coin, 999)

# =========================
# 📣 تلغرام + السجل
# =========================
def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}"); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text}, timeout=6)
    except Exception as e:
        print("Telegram error:", e)

def send_long_message(text):
    chunk = 3500
    for i in range(0, len(text), chunk):
        send_message(text[i:i+chunk])

def log_alert(entry: dict):
    try:
        r.lpush("alerts", json.dumps(entry, ensure_ascii=False))
        r.ltrim("alerts", 0, 49)
    except Exception as e:
        print("log_alert error:", e)

def already_alerted_today(coin): return r.exists(f"alerted:{coin}") == 1
def mark_alerted_today(coin):     r.setex(f"alerted:{coin}", ALERT_EXPIRE_SEC, "1")
def is_log_stream_on():           return r.get("log_stream") == b"on"
def set_log_stream(on: bool):     r.set("log_stream", "on" if on else "off")

def format_alert_line(a, idx=None):
    tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.get("ts", 0))) if a.get("ts") else "?"
    line = f"{tstr}  |  {a.get('coin','?'):>6}  |  #{a.get('rank','?'):<3}  |  {a.get('tag','?'):>6}  | heat={a.get('heat','?')}"
    return (f"{idx:02d}. " if idx is not None else "") + line

def dump_last_alerts_text(limit=50):
    items = []
    for raw in r.lrange("alerts", 0, limit-1):
        try: items.append(json.loads(raw))
        except: pass
    if not items: return "لا يوجد سجل بعد."
    lines = ["📒 آخر التنبيهات:"]
    for i, a in enumerate(items, 1):
        lines.append(format_alert_line(a, i))
    return "\n".join(lines)

def notify_buy(coin, tag, change_text=None):
    if already_alerted_today(coin): return
    if is_recent_exploder(coin):    return
    revive_ok = is_reviving(coin)
    if REVIVE_ONLY and not revive_ok: return
    rank = get_rank_from_bitvavo(coin)
    if rank > RANK_FILTER: return
    now = time.time()
    if coin in last_alert and now - last_alert[coin] < BUY_COOLDOWN_SEC: return
    last_alert[coin] = now
    mark_alerted_today(coin)

    tag_txt = f"{tag}" + (" • revive" if revive_ok else "")
    msg = f"🚀 {coin} {tag_txt} #top{rank}"
    if change_text: msg = f"🚀 {coin} {change_text} #top{rank}"
    send_message(msg)

    if SAQAR_WEBHOOK:
        try:
            requests.post(SAQAR_WEBHOOK, json={"message":{"text": f"اشتري {coin}"}}, timeout=5)
        except Exception: pass

    entry = {"ts": int(now), "coin": coin, "rank": rank, "tag": tag_txt, "heat": round(heat_ewma, 4)}
    log_alert(entry)
    if is_log_stream_on(): send_message("🧾 " + format_alert_line(entry))

# =========================
# 🔥 حرارة السوق + تكييف
# =========================
def compute_market_heat():
    global heat_ewma
    now = time.time(); moved = 0; total = 0
    with lock: wl = list(watchlist)
    for c in wl:
        dq = prices[c]
        if len(dq) < 2: continue
        old = None; cur = dq[-1][1]
        for ts, pr in reversed(dq):
            if now - ts >= 60: old = pr; break
        if old and old > 0:
            ret = (cur - old) / old * 100.0
            total += 1
            if abs(ret) >= HEAT_RET_PCT: moved += 1
    raw = (moved / total) if total else 0.0
    heat_ewma = (1-HEAT_SMOOTH)*heat_ewma + HEAT_SMOOTH*raw if total else heat_ewma
    return heat_ewma

def adaptive_multipliers():
    h = max(0.0, min(1.0, heat_ewma))
    return 0.75 if h < 0.15 else 0.9 if h < 0.35 else 1.0 if h < 0.6 else 1.25

def market_snapshot_5m():
    """
    لقطة سريعة لعوائد ~5 دقائق من الذاكرة المحلية.
    يرجّع dict فيه الرابحين/الخاسرين وأقوى/أضعف.
    """
    now = time.time()
    snaps = []  # [(coin, ret_pct)]
    with lock:
        wl = list(watchlist)
    for c in wl:
        dq = prices[c]
        if not dq:
            continue
        cur = dq[-1][1]
        old = None
        for ts, pr in reversed(dq):
            if now - ts >= 270:  # ~4.5m وما أقدم من هيك بكثير
                old = pr
                break
        if old and old > 0:
            ret = (cur - old) / old * 100.0
            snaps.append((c, ret))

    if not snaps:
        return {"gainers": 0, "losers": 0, "top": None, "bottom": None}

    snaps.sort(key=lambda x: x[1], reverse=True)
    gainers = sum(1 for _, r in snaps if r > 0)
    losers  = sum(1 for _, r in snaps if r < 0)
    top     = {"coin": snaps[0][0], "ret": round(snaps[0][1], 3)}
    bottom  = {"coin": snaps[-1][0], "ret": round(snaps[-1][1], 3)}

    return {"gainers": gainers, "losers": losers, "top": top, "bottom": bottom}

# =========================
# 🧩 أنماط
# =========================
def check_top10_pattern(coin, m):
    thresh = BASE_STEP_PCT * m
    now = time.time(); dq = prices[coin]
    if len(dq) < 2: return False
    start_ts = now - STEP_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3: return False
    p0 = window[0][1]; step1 = False; last_p = p0
    for ts, pr in window[1:]:
        ch1 = (pr - p0) / p0 * 100.0
        if not step1 and ch1 >= thresh:
            step1 = True; last_p = pr; continue
        if step1:
            ch2 = (pr - last_p) / last_p * 100.0
            if ch2 >= thresh: return True
            if (pr - last_p) / last_p * 100.0 <= -thresh:
                step1 = False; p0 = pr
    return False

def check_top1_pattern(coin, m):
    seq_parts = [float(x.strip()) for x in BASE_STRONG_SEQ.split(",") if x.strip()]
    seq_parts = [x * m for x in seq_parts]
    now = time.time(); dq = prices[coin]
    if len(dq) < 2: return False
    start_ts = now - SEQ_WINDOW_SEC
    window = [(ts, p) for ts, p in dq if ts >= start_ts]
    if len(window) < 3: return False
    slack = 0.3 * m; base_p = window[0][1]; step_i = 0; peak_after_step = base_p
    for ts, pr in window[1:]:
        ch = (pr - base_p) / base_p * 100.0; need = seq_parts[step_i]
        if ch >= need:
            step_i += 1; base_p = pr; peak_after_step = pr
            if step_i == len(seq_parts): return True
        else:
            if peak_after_step > 0:
                drop = (pr - peak_after_step) / peak_after_step * 100.0
                if drop <= -(slack): base_p = pr; peak_after_step = pr; step_i = 0
    return False

# =========================
# 🔁 العمال
# =========================
def room_refresher():
    while True:
        try:
            new_syms = get_5m_top_symbols(limit=MAX_ROOM)
            with lock:
                for s in new_syms: watchlist.add(s)
                if len(watchlist) > MAX_ROOM:
                    ranked = sorted(list(watchlist), key=lambda c: get_rank_from_bitvavo(c))
                    watchlist.clear()
                    for c in ranked[:MAX_ROOM]: watchlist.add(c)
            last_beats["room"] = time.time()
        except Exception as e:
            print("room_refresher error:", e)
        time.sleep(BATCH_INTERVAL_SEC)

def price_poller():
    while True:
        now = time.time()
        try:
            with lock: syms = list(watchlist)
            for s in syms:
                pr = get_price(s)
                if pr is None: continue
                dq = prices[s]; dq.append((now, pr))
                cutoff = now - 1200
                while dq and dq[0][0] < cutoff: dq.popleft()
            last_beats["price"] = time.time()
        except Exception as e:
            print("price_poller error:", e)
        time.sleep(SCAN_INTERVAL)

def analyzer():
    while True:
        if time.time() - start_time < GLOBAL_WARMUP_SEC:
            time.sleep(1); continue
        try:
            compute_market_heat(); m = adaptive_multipliers()
            with lock: syms = list(watchlist)
            for s in syms:
                if check_top1_pattern(s, m):
                    notify_buy(s, tag="top1"); continue
                if check_top10_pattern(s, m):
                    notify_buy(s, tag="top10")
            last_beats["analyzer"] = time.time()
        except Exception as e:
            print("analyzer error:", e)
        time.sleep(1)

# =========================
# 🌐 مسارات
# =========================
@app.route("/", methods=["GET"])
def health(): return "Predictor bot is alive ✅", 200

@app.route("/status", methods=["GET"])
def status():
    m = adaptive_multipliers()
    with lock: wl = list(watchlist)
    now = time.time()
    snap = market_snapshot_5m()
    return {
        "message": "OK",
        "heat": round(heat_ewma, 4),
        "multiplier": m,
        "watchlist_size": len(wl),
        "rank_filter": RANK_FILTER,
        "lastday_skip_pct": LASTDAY_SKIP_PCT,
        "revive_only": bool(REVIVE_ONLY),
        "log_stream": is_log_stream_on(),
        "beats_sec_ago": {
            "room": round(now - last_beats["room"],1) if last_beats["room"] else None,
            "price": round(now - last_beats["price"],1) if last_beats["price"] else None,
            "analyzer": round(now - last_beats["analyzer"],1) if last_beats["analyzer"] else None,
        },
        "snapshot_5m": snap
    }, 200

# ============== Webhook تلغرام (رد فوري + تنفيذ بالخلفية) ==============
OPEN_ALIASES   = {"/افتح السجل","افتح السجل","/openlog","openlog"}
CLOSE_ALIASES  = {"/اغلق السجل","اغلق السجل","/closelog","closelog"}
STATUS_ALIASES = {"/status","شو عم تعمل","/شو_عم_تعمل","شو عم_تعمل","/ping","بينغ","بنج"}

def handle_cmd(chat_id, low):
    try:
        if low in OPEN_ALIASES:
            set_log_stream(True); send_message("📒 تم فتح السجل. (سيتم بث أي تنبيه جديد هنا)")
            send_long_message(dump_last_alerts_text(50))
        elif low in CLOSE_ALIASES:
            set_log_stream(False); send_message("✅ تم إغلاق السجل.")
        elif low in STATUS_ALIASES:
            now = time.time()
            beats = {k: (round(now - v,1) if v else None) for k,v in last_beats.items()}
            with lock: wl = list(watchlist)
            m = adaptive_multipliers()
            snap = market_snapshot_5m()
            top_txt = f"{snap['top']['coin']} {snap['top']['ret']}%" if snap['top'] else "—"
            bot_txt = f"{snap['bottom']['coin']} {snap['bottom']['ret']}%" if snap['bottom'] else "—"
            send_message(
                f"ℹ️ الحالة:\n"
                f"- heat={round(heat_ewma,4)} | m={m}\n"
                f"- watchlist={len(wl)} | rank_filter={RANK_FILTER}\n"
                f"- revive_only={bool(REVIVE_ONLY)} | lastday_skip={LASTDAY_SKIP_PCT}%\n"
                f"- beats: room={beats['room']}s, price={beats['price']}s, analyzer={beats['analyzer']}s\n"
                f"- 5m: gainers={snap['gainers']} | losers={snap['losers']} | top={top_txt} | bottom={bot_txt}\n"
                f"- log_stream={'on' if is_log_stream_on() else 'off'}"
            )
        else:
            if low.startswith("/"):
                send_message("❔ أمر غير معروف. جرّب: /افتح السجل أو /اغلق السجل أو /status")
    except Exception as e:
        print("handle_cmd error:", e)

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg = data.get("message") or data.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "")

        print(f"[TG] from={chat_id} text={repr(text)}")

        if CHAT_ID and chat_id and chat_id != str(CHAT_ID):
            return {"ok": True}, 200

        HIDDEN = ["\u200e","\u200f","\u202a","\u202b","\u202c","\u202d","\u202e","\u200d","\u061C","ـ"]
        low = text
        for h in HIDDEN: low = low.replace(h, "")
        low = " ".join(low.split()).lower()

        Thread(target=handle_cmd, args=(chat_id, low), daemon=True).start()
    except Exception as e:
        print("telegram_webhook error:", e)
    return {"ok": True}, 200

# =========================
# 🚀 تشغيل العمال مرة واحدة (ضروري لـ gunicorn)
# =========================
threads_started = False
threads_lock = Lock()

def start_workers_once():
    global threads_started
    with threads_lock:
        if threads_started: return
        Thread(target=room_refresher, daemon=True).start()
        Thread(target=price_poller, daemon=True).start()
        Thread(target=analyzer, daemon=True).start()
        threads_started = True
        print("[BOOT] ✅ background workers started")

# شغّلها عند الاستيراد + قبل أول طلب
start_workers_once()

# للتشغيل المحلي فقط
if __name__ == "__main__":
    start_workers_once()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))