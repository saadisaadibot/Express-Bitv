# Express Momentum v1 — Pure Jump Hunter (saqar-style webhook)
# - زخم صرف: قفزة 1–3–5 دقائق + تسارع + اختراق قمة قريبة
# - لا مؤشرات ثقيلة ولا فلاتر “تخنق” — فقط sanity خفيفة (سبريد/كتاب).
# - /scan يدوي، autoscan عند /ready، /health. Telegram اختياري.

import os, time, threading, requests
from flask import Flask, request, jsonify

# ===== ENV =====
BITVAVO = "https://api.bitvavo.com/v2"
BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
SAQAR_URL   = os.getenv("SAQAR_WEBHOOK","").strip().rstrip("/")

AUTOSCAN_ON_READY = int(os.getenv("AUTOSCAN_ON_READY","1"))
AGGR              = int(os.getenv("AGGRESSIVE","1"))

BUY_EUR   = float(os.getenv("BUY_EUR","25"))

# عتبات اندفاع (٪). خفّضها لعدوانية أعلى.
JUMP_1M = float(os.getenv("JUMP_1M","0.35"))     # تغير آخر دقيقة
JUMP_3M = float(os.getenv("JUMP_3M","1.10"))     # تغير 3 دقائق
JUMP_5M = float(os.getenv("JUMP_5M","1.80"))     # تغير 5 دقائق
ACC_MIN = float(os.getenv("ACC_MIN","0.15"))     # تسارع بسيط: Δ(آخر دقيقة - التي قبلها)

# اختراق/سحب بسيط: لازم الإغلاق قريب من قمة آخر N دقائق
HH_N_MIN     = int(os.getenv("HH_N_MIN","8"))    # نافذة القمة بالدقائق
PULLBACK_TOL = float(os.getenv("PULLBACK_TOL","0.30"))  # سماح نزول عن القمة (٪)

# sanity guards خفيفة (تقدر تعطلها بوضع قيم كبيرة)
MAX_SPREAD_HARD = float(os.getenv("MAX_SPREAD_HARD","1.20"))   # ٪
DEPTH_MIN_EUR   = float(os.getenv("DEPTH_MIN_EUR","300"))      # EUR asks

# سرعات/أحجام سكان
MARKETS_REFRESH_SEC = int(os.getenv("MARKETS_REFRESH_SEC","45"))
HOT_SIZE    = int(os.getenv("HOT_SIZE","20"))
SCOUT_SIZE  = int(os.getenv("SCOUT_SIZE","80"))

# تبريد ومنع تكرار
ERROR_COOLDOWN_SEC = int(os.getenv("ERROR_COOLDOWN_SEC","60"))
MIN_COOLDOWN_READY_SEC = int(os.getenv("MIN_COOLDOWN_READY_SEC","20"))
COOLDOWN_SAME_SEC  = int(os.getenv("COOLDOWN_SAME_SEC","25"))   # لكل عملة بعد إرسال

# ===== حالة =====
RUN_ID = 0
LAST_SIGNAL_TS = 0
_LAST_ERR = {}
TRADES_BAN_UNTIL = {}
COOLDOWN_UNTIL = {}  # coin -> ts

app = Flask(__name__)

# ===== Telegram =====
def tg_send(txt: str):
    if not BOT_TOKEN:
        print("TG:", txt); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": txt},
            timeout=5
        )
    except Exception as e:
        print("tg_send err:", e)

# ===== أخطاء =====
def _should_report(key: str) -> bool:
    ts = _LAST_ERR.get(key, 0)
    if time.time() - ts >= ERROR_COOLDOWN_SEC:
        _LAST_ERR[key] = time.time(); return True
    return False

def report_error(tag: str, detail: str):
    if _should_report(f"{tag}:{detail[:80]}"):
        tg_send(f"🛑 {tag} — {detail}")

def _url_ok(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("http://","https://"))

if not _url_ok(SAQAR_URL):
    report_error("config","SAQAR_WEBHOOK غير مضبوط — لن تُرسل إشارات.")

# ===== Bitvavo =====
def bv_safe(path, timeout=5, params=None, tag=None):
    tag = tag or path
    try:
        r = requests.get(f"{BITVAVO}{path}", params=params, timeout=timeout)
        if not (200 <= r.status_code < 300): return None
        return r.json()
    except Exception:
        return None

def list_markets_eur():
    rows = bv_safe("/markets", tag="/markets") or []
    out=[]
    for r in rows:
        try:
            if r.get("quote")!="EUR": continue
            m=r.get("market"); b=r.get("base")
            minq=float(r.get("minOrderInQuoteAsset",0) or 0)
            if not m or not b: continue
            out.append((m,b,float(r.get("pricePrecision",6)), minq))
        except Exception as e:
            report_error("parse /markets", f"{type(e).__name__}: {e}")
    return out

def book(market, depth=2):
    data = bv_safe(f"/{market}/book", params={"depth": depth}, tag=f"/book {market}")
    if not isinstance(data, dict): return None
    try:
        bids=[]; asks=[]
        for row in data.get("bids") or []:
            if len(row)>=2: bids.append((float(row[0]), float(row[1])))
        for row in data.get("asks") or []:
            if len(row)>=2: asks.append((float(row[0]), float(row[1])))
        if not bids or not asks: return None
        best_bid = bids[0][0]; best_ask = asks[0][0]
        spread = (best_ask-best_bid)/max(best_bid,1e-12)*100.0
        depth_ask = sum(p*a for p,a in asks[:depth])
        return {"bid":best_bid,"ask":best_ask,"spread_pct":spread,"depth_ask_eur":depth_ask}
    except Exception as e:
        report_error("parse /book", f"{market} {type(e).__name__}: {e}"); return None

def candles(market, interval="1m", limit=12):
    data = bv_safe(f"/{market}/candles", params={"interval":interval,"limit":limit}, tag=f"/candles {market}")
    return data if isinstance(data, list) else []

# ===== زخم صرف من الشموع =====
def pct(a,b): 
    try: return (a/b - 1.0)*100.0
    except: return 0.0

def jumps_meta(cs1m):
    # cs1m: [[t,o,h,l,c,v], ...]
    try:
        closes=[float(r[4]) for r in cs1m]
        highs =[float(r[2]) for r in cs1m]
    except: 
        return 0.0,0.0,0.0,0.0,0.0
    if len(closes) < 7:
        return 0.0,0.0,0.0,0.0,0.0
    ch1 = pct(closes[-1], closes[-2])
    ch2 = pct(closes[-2], closes[-3])
    ch3 = pct(closes[-1], closes[-4])   # ~3 دقائق
    ch5 = pct(closes[-1], closes[-6])   # ~5 دقائق
    hhN = max(highs[-(HH_N_MIN+1):-1]) if len(highs) >= HH_N_MIN+1 else max(highs[:-1])
    pull = pct(hhN, closes[-1])         # كم نحن تحت القمة (٪)
    return ch1, ch2, ch3, ch5, pull

def momentum_hit(market):
    cs = candles(market,"1m", limit=max(12, HH_N_MIN+2))
    if not cs or len(cs)<7: 
        return False, "", {}
    ch1, ch2, ch3, ch5, pull = jumps_meta(cs)
    acc = ch1 - max(0.0, ch2)  # تسارع بسيط: آخر دقيقة أقوى من اللي قبلها

    # اختراق: لازم نكون قريبين جداً من قمة النافذة
    near_hh = (pull <= PULLBACK_TOL)

    # عتبات عدوانية أخف بالوضع AGGR
    j1 = JUMP_1M * (0.85 if AGGR else 1.0)
    j3 = JUMP_3M * (0.90 if AGGR else 1.0)
    j5 = JUMP_5M * (0.92 if AGGR else 1.0)
    a0 = ACC_MIN * (0.85 if AGGR else 1.0)

    ok = (ch1 >= j1 and ch3 >= j3 and ch5 >= j5 and acc >= a0 and near_hh)

    why = f"Δ1m={ch1:.2f}% Δ3m={ch3:.2f}% Δ5m={ch5:.2f}% acc={acc:.2f}% pull={pull:.2f}%"
    meta={"ch1":ch1,"ch3":ch3,"ch5":ch5,"acc":acc,"pull":pull}
    return ok, why, meta

# ===== sanity خفيفة جداً =====
def sanity_ok(market):
    bk = book(market,2)
    if not bk: return False, "no_book"
    if bk["spread_pct"] > MAX_SPREAD_HARD: return False, f"spread>{MAX_SPREAD_HARD}%"
    if bk["depth_ask_eur"] < DEPTH_MIN_EUR: return False, "thin_asks"
    return True, "ok"

# ===== إرسال لصقر =====
def send_buy(coin, why_line):
    global LAST_SIGNAL_TS
    if not _url_ok(SAQAR_URL):
        report_error("send_buy","SAQAR_WEBHOOK غير صالح."); return
    url = SAQAR_URL + "/hook"
    payload = {"action":"buy","coin":coin.upper()}
    try:
        r = requests.post(url, json=payload, timeout=(6,20))
        if 200 <= r.status_code < 300:
            LAST_SIGNAL_TS = time.time()
            COOLDOWN_UNTIL[coin.upper()] = time.time() + COOLDOWN_SAME_SEC
            tg_send(f"🚀 BUY→ {coin} — {why_line}")
        else:
            report_error("send_buy", f"HTTP {r.status_code} | {r.text[:140]}")
    except Exception as e:
        report_error("send_buy", f"{type(e).__name__}: {e}")

# ===== ترتيب حسب سيولة خفيفة =====
def sort_by_liq(markets):
    # نستخدم spread/ask depth كوكيل بسيط
    scored=[]
    for m in markets:
        b = book(m,1)
        if not b: continue
        score = (b["depth_ask_eur"] / max(1.0, b["spread_pct"]+0.05))
        scored.append((m, score))
    scored.sort(key=lambda x:x[1], reverse=True)
    return [m for m,_ in scored]

# ===== اختيار وإطلاق (Momentum-only) =====
def pick_and_emit(markets):
    best=None
    for m in markets:
        coin = m.split("-")[0]
        if COOLDOWN_UNTIL.get(coin,0) > time.time(): 
            continue
        ok_sanity, why_s = sanity_ok(m)
        if not ok_sanity:
            continue
        ok, why, meta = momentum_hit(m)
        if ok:
            # “نطلق ونمشي” — أول Hit يكفي
            send_buy(coin, f"{why}")
            return True
        # وإلا احفظ أعلى اندفاع (احتياط)
        score = max(0.0, meta.get("ch1",0)*0.5 + meta.get("ch3",0)*0.3 + meta.get("ch5",0)*0.2 + max(0.0,meta.get("acc",0))*0.2)
        if not best or score > best[0]:
            best=(score, coin, why)
    # إذا مافي Hit صريح، خُذ الأفضل لو تعدّى بعض الزخم
    if best and best[0] >= (JUMP_3M*0.6):
        send_buy(best[1], f"soft-hit {best[2]}")
        return True
    return False

# ===== حلقة السكان =====
def scanner_loop(run_id):
    tg_send(f"🔎 Momentum scan run={run_id}")
    try:
        mkts_raw = list_markets_eur()
        mkts = [m for (m,b,pp,minq) in mkts_raw if m.endswith("-EUR") and minq<=50.0]
        HOT   = sort_by_liq(mkts)[:HOT_SIZE]
        SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]
        hot_t=scout_t=refresh_t=0
        while run_id == RUN_ID:
            if time.time() - LAST_SIGNAL_TS < MIN_COOLDOWN_READY_SEC:
                time.sleep(0.10); continue
            try:
                if time.time()-hot_t >= 0.9:
                    if pick_and_emit(HOT): return
                    hot_t=time.time()
            except Exception as e:
                report_error("hot", f"{type(e).__name__}: {e}")
            try:
                if time.time()-scout_t >= (2.0 if AGGR else 3.2):
                    if pick_and_emit(SCOUT): return
                    scout_t=time.time()
            except Exception as e:
                report_error("scout", f"{type(e).__name__}: {e}")
            try:
                if time.time()-refresh_t >= MARKETS_REFRESH_SEC:
                    mkts_new = [m for (m,b,pp,minq) in list_markets_eur() if m.endswith("-EUR")]
                    if mkts_new:
                        mkts = mkts_new
                        HOT   = sort_by_liq(mkts)[:HOT_SIZE]
                        SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]
                    refresh_t=time.time()
            except Exception as e:
                report_error("refresh", f"{type(e).__name__}: {e}")
            time.sleep(0.03 if AGGR else 0.06)
        tg_send(f"⏹️ run={run_id} stopped (run={RUN_ID})")
    except Exception as e:
        report_error("scanner crash", f"{type(e).__name__}: {e}")

# ===== Flask =====
@app.route("/webhook", methods=["POST"])
def tg_webhook():
    global RUN_ID
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or upd.get("edited_message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text: return jsonify(ok=True)
    if text.startswith("/scan"):
        RUN_ID += 1
        threading.Thread(target=scanner_loop, args=(RUN_ID,), daemon=True).start()
        tg_send("✅ Momentum scan بدأ.")
    return jsonify(ok=True)

@app.route("/ready", methods=["POST"])
def on_ready():
    global RUN_ID, LAST_SIGNAL_TS
    data = request.get_json(silent=True) or {}
    coin  = (data.get("coin") or "").upper()
    reason= data.get("reason"); pnl=data.get("pnl_eur")
    tg_send(f"📩 Ready من صقر — {coin} ({reason}) pnl={pnl}")
    # تبريد بسيط بعد ready
    LAST_SIGNAL_TS = time.time()
    if AUTOSCAN_ON_READY:
        RUN_ID += 1
        threading.Thread(target=scanner_loop, args=(RUN_ID,), daemon=True).start()
    return jsonify(ok=True)

@app.route("/health", methods=["GET"])
def health():
    return jsonify(ok=True, run_id=RUN_ID, last_signal_ts=LAST_SIGNAL_TS,
                   cooldown=len(COOLDOWN_UNTIL), aggr=AGGR), 200

@app.route("/", methods=["GET"])
def home():
    return f"Express Momentum v1 ✅ run={RUN_ID} | aggr={AGGR}", 200

# ===== Main =====
if __name__=="__main__":
    port = int(os.getenv("PORT","8082"))
    tg_send("⚡️ Express Momentum v1 — started (pure jumps).")
    app.run("0.0.0.0", port, threaded=True)