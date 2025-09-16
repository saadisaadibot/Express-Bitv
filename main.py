# -*- coding: utf-8 -*-
"""
Abosiyah Pro v3 — 15m Profit Hunter
- طبقات: Hot(1s) + Scout(5s) + NewListings(60s) + Gap-Sniper
- score(top1) + quality guards + ttl_sec قصير
- تعلم تكيفي من نتائج صقر (coin EMA)
- /scan يدوي للطوارئ + autoscan عند /ready (اختياري)

ENV:
  BOT_TOKEN, CHAT_ID
  SAQER_HOOK_URL, LINK_SECRET
  AUTOSCAN_ON_READY=1
  SCORE_STAR=0.65 (balanced)
  MAX_SPREAD=0.22
  MAX_SLIP=0.15
  DEPTH_MIN_EUR=3000
  TTL_SEC=60
  TP_EUR_HINT=0.06
  MIN_COOLDOWN_READY_SEC=30
  MIN_COOLDOWN_FAIL_MIN=30
  BUY_EUR=25             # تقدير مبلغ الشراء لحساب الانزلاق
  MARKETS_REFRESH_SEC=60
  HOT_SIZE=18            # كم سوق بأعلى سيولة نراقب/نرتّب كل 1s
  SCOUT_SIZE=60          # توسيع البحث كل 5s
"""

import os, time, threading, requests, math, statistics as st
from flask import Flask, request, jsonify

# ===== إعدادات =====
BITVAVO = "https://api.bitvavo.com/v2"
BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
SAQER_HOOK_URL = os.getenv("SAQER_HOOK_URL","").strip()
LINK_SECRET = os.getenv("LINK_SECRET","").strip()

AUTOSCAN_ON_READY = int(os.getenv("AUTOSCAN_ON_READY","1"))
SCORE_STAR  = float(os.getenv("SCORE_STAR","0.65"))
MAX_SPREAD  = float(os.getenv("MAX_SPREAD","0.22"))
MAX_SLIP    = float(os.getenv("MAX_SLIP","0.15"))
DEPTH_MIN_EUR = float(os.getenv("DEPTH_MIN_EUR","3000"))
TTL_SEC     = int(os.getenv("TTL_SEC","60"))
TP_EUR_HINT = float(os.getenv("TP_EUR_HINT","0.06"))
MIN_COOLDOWN_READY_SEC = int(os.getenv("MIN_COOLDOWN_READY_SEC","30"))
MIN_COOLDOWN_FAIL_MIN  = int(os.getenv("MIN_COOLDOWN_FAIL_MIN","30"))
BUY_EUR     = float(os.getenv("BUY_EUR","25"))
MARKETS_REFRESH_SEC = int(os.getenv("MARKETS_REFRESH_SEC","60"))
HOT_SIZE    = int(os.getenv("HOT_SIZE","18"))
SCOUT_SIZE  = int(os.getenv("SCOUT_SIZE","60"))

# جداول حالة بسيطة
RUN_ID = 0
COOLDOWN_UNTIL = {}   # coin -> ts
LEARN = {}            # coin -> {"pnl_ema":..., "win_ema":..., "latency_ema":..., "adj":...}
LAST_SIGNAL_TS = 0

app = Flask(__name__)

def tg_send(txt):
    if not BOT_TOKEN: 
        print("TG:", txt); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": txt}, timeout=6)
    except Exception as e:
        print("tg_send err:", e)

# ===== Bitvavo helpers (قراءة عامة) =====
def bv(path, timeout=6, params=None):
    r = requests.get(f"{BITVAVO}{path}", params=params, timeout=timeout)
    try: return r.json()
    except: return None

def list_markets_eur():
    rows = bv("/markets") or []
    out=[]
    for r in rows:
        try:
            if r.get("quote")!="EUR": continue
            m=r.get("market"); b=r.get("base"); s=float(r.get("status","1")!="halted")
            minq=float(r.get("minOrderInQuoteAsset",0) or 0)
            if not m or not b: continue
            out.append((m,b,float(r.get("pricePrecision",6)), minq))
        except: 
            continue
    return out

def book(market, depth=3):
    data = bv(f"/{market}/book", params={"depth": depth})
    if not isinstance(data, dict): return None
    try:
        bids = [(float(p), float(a)) for p,a,_ in data.get("bids",[])]
        asks = [(float(p), float(a)) for p,a,_ in data.get("asks",[])]
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        spread = (best_ask-best_bid)/best_bid*100 if (best_bid>0 and best_ask>0) else 9e9
        depth_bid = sum(p*a for p,a in bids[:3])
        depth_ask = sum(p*a for p,a in asks[:3])
        return {"bid":best_bid,"ask":best_ask,"spread_pct":spread,"depth_bid_eur":depth_bid,"depth_ask_eur":depth_ask,
                "bids":bids,"asks":asks}
    except:
        return None

def trades(market, limit=50):
    data = bv(f"/trades", params={"market": market, "limit": limit})
    return data if isinstance(data, list) else []

def candles(market, interval="1m", limit=240):
    data = bv(f"/{market}/candles", params={"interval":interval,"limit":limit})
    return data if isinstance(data, list) else []

# ===== تقديرات سريعة =====
def estimate_slippage_pct(asks, want_eur: float):
    # كم % صعود يُتوقع عند أكل الـ ask حتى مبلغ want_eur ؟ (تقريب)
    if not asks or want_eur<=0: return 9e9
    tot_eur = 0.0; first = asks[0][0]; last=first
    for p,a in asks:
        val = p*a
        take = min(val, max(0.0, want_eur - tot_eur))
        if take<=0: break
        fill_base = take / p
        tot_eur += fill_base * p
        last = p
        if tot_eur >= want_eur: break
    if first<=0: return 9e9
    return (last/first - 1.0)*100.0

def uptick_ratio(trs):
    # نسبة صفقات على الـ Ask مقابل المجموع (تقريب عبر side)
    if not trs: return 0.0
    upt = sum(1 for t in trs if (t.get("side","").lower()=="buy"))
    return upt / max(1, len(trs))

def trades_10s_speed(trs, now_ms):
    recent = [t for t in trs if (now_ms - int(t.get("timestamp",0) or 0)) <= 10_000]
    return len(recent) / 10.0

def vwap5m(market):
    cs = candles(market,"5m", limit=1)
    # Bitvavo candle row: [timestamp, open, high, low, close, volume]
    try:
        c = cs[-1]; return float(c[4])
    except: return 0.0

def h15_breakout(market):
    cs = candles(market,"1m", limit=16)
    try:
        highs = [float(r[2]) for r in cs[:-1]]
        last  = float(cs[-1][4])
        return last, (last - max(highs))/max(1e-12, max(highs)) * 100.0
    except: return 0.0, 0.0

def adx_rsi_lite(market):
    cs = candles(market,"1m", limit=240)
    try:
        closes=[float(r[4]) for r in cs]
        highs =[float(r[2]) for r in cs]
        lows  =[float(r[3]) for r in cs]
    except: 
        return 0.0, 50.0
    if len(closes)<60: return 0.0, 50.0
    # RSI 14
    gains=loss=0.0
    for i in range(-14,0):
        d = closes[i]-closes[i-1]
        gains += d if d>=0 else 0.0
        loss  += -d if d<0 else 0.0
    avg_gain = gains/14
    avg_loss = loss/14 if loss>0 else 1e-9
    rs = avg_gain/avg_loss
    rsi = 100.0 - (100.0 / (1.0+rs))
    # ADX lite: TR المتوسط ونطاق الاتجاه (تقريب)
    trs=[]
    for i in range(1,len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    if len(trs)<20: return 0.0, rsi
    atr = sum(trs[-14:])/14
    adx = min(40.0, max(0.0, (atr/max(1e-9, closes[-1]))*1000*1.2))  # تقريب خفيف
    return adx, rsi

# ===== Multi-Score =====
def score_market(market, cache):
    now_ms = int(time.time()*1000)
    bk = cache.get(("book",market)) or book(market,3); cache[("book",market)] = bk
    trs = cache.get(("trades",market))
    if trs is None or (now_ms - int(trs[0]["timestamp"]) if (isinstance(trs,list) and trs) else 9e9) > 2000:
        trs = trades(market, 60); cache[("trades",market)] = trs

    if not bk or bk["bid"]<=0 or bk["ask"]<=0: return 0.0, {"why":"no_book"}
    spread = bk["spread_pct"]
    depthA = bk["depth_ask_eur"]; depthB = bk["depth_bid_eur"] = bk["depth_bid_eur"] if "depth_bid_eur" in bk else bk.get("depth_bid_eur",0.0)
    # uptick & speed
    ur = uptick_ratio(trs)
    spd = trades_10s_speed(trs, now_ms)
    # breakout
    last, bo15 = h15_breakout(market)
    vw = vwap5m(market)
    above_vwap = 1.0 if (vw>0 and last>=vw) else 0.0
    # regime
    adx, rsi = adx_rsi_lite(market)
    # slip estimate
    slip = estimate_slippage_pct(bk["asks"], BUY_EUR)

    # z-scores مبسطة (0..1)
    z_tape = min(1.0, 0.5*ur + 0.5*min(1.0, spd/2.0))
    imb = depthB / max(1.0, (depthA+depthB))
    z_imb  = max(0.0, (imb - 0.45)/0.25)  # 0 عند 0.45 → 1 عند 0.70
    z_break= max(0.0, min(1.0, (bo15/0.5))) * 0.7 + above_vwap*0.3
    z_vol  = min(1.0, adx/30.0)
    z_reg  = 1.0 if (55<=rsi<=75) else (0.6 if 50<=rsi<55 or 75<rsi<=80 else 0.2)

    score = 0.35*z_tape + 0.25*z_imb + 0.20*z_break + 0.10*z_vol + 0.10*z_reg

    why = f"ur={ur:.2f},spd={spd:.2f},imb={imb:.2f},bo15={bo15:.2f}%,adx~{adx:.1f},rsi={rsi:.0f},spr={spread:.2f}%,slip~{slip:.2f}%"
    meta = {"why": why, "spread": spread, "slip": slip, "imb": imb, "ur": ur, "spd": spd}
    return score, meta

# ===== Gap-Sniper =====
def gap_sniper(market, cache):
    bk = cache.get(("book",market)) or book(market,3); cache[("book",market)] = bk
    if not bk: return 0.0, {}
    # لقطة: ask ضعيف (depth_ask صغير) + uptick عالي + spread ضيق → ضغط سريع للأعلى
    depthA = bk["depth_ask_eur"]; depthB = bk["depth_bid_eur"]
    if depthA<=0 or depthB<=0: return 0.0, {}
    ratio = depthB / max(1e-9, depthA)
    spr = bk["spread_pct"]
    score = 0.0
    if spr <= MAX_SPREAD and ratio >= 2.0:
        score = min(1.0, (ratio-2.0)/3.0 + 0.5)  # يبدأ 0.5 عند 2x ويصعد
    return score, {"why": f"gap ratio={ratio:.2f}, spr={spr:.2f}%"}

# ===== قاردات الجودة =====
def quality_guards(market, meta):
    # spread/slip/depth/cooldown
    bk = book(market,3)
    if not bk: return False, "no_book"
    if bk["spread_pct"] > MAX_SPREAD: return False, "spread"
    if bk["depth_ask_eur"] < DEPTH_MIN_EUR: return False, "depth"
    slip_pct = meta.get("slip", 9e9)
    if slip_pct > MAX_SLIP: return False, "slip"
    coin = market.split("-")[0]
    if COOLDOWN_UNTIL.get(coin,0) > time.time(): return False, "cooldown"
    return True, "ok"

# ===== تعلّم بسيط لكل عملة =====
def learn_update(coin, pnl_eur, reason):
    L = LEARN.get(coin, {"pnl_ema":0.0,"win_ema":0.5,"adj":0.0})
    alpha=0.3
    L["pnl_ema"] = (1-alpha)*L["pnl_ema"] + alpha*(pnl_eur or 0.0)
    if reason in ("tp_filled", "manual_sell_filled"):
        L["win_ema"] = (1-alpha)*L["win_ema"] + alpha*1.0
    else:
        L["win_ema"] = (1-alpha)*L["win_ema"] + alpha*0.0
    # تعديل عتبة العملة: إن كانت تربح، خفّض شرطها قليلًا؛ وإن كانت تخسر، ارفعه
    L["adj"] = max(-0.05, min(0.08, 0.08*(0.5 - L["win_ema"])))
    LEARN[coin]=L

def adjusted_s_star(coin):
    adj = (LEARN.get(coin) or {}).get("adj", 0.0)
    return max(0.52, min(0.90, SCORE_STAR + adj))

# ===== إرسال الإشارة لصقر =====
def send_buy(coin, score, why):
    global LAST_SIGNAL_TS
    body = {
        "action":"buy",
        "coin": coin,
        "ttl_sec": TTL_SEC,
        "confidence": round(score,3),
        "tp_eur_hint": TP_EUR_HINT,
        "why": why
    }
    headers={"Content-Type":"application/json"}
    if LINK_SECRET: headers["X-Link-Secret"]=LINK_SECRET
    try:
        r = requests.post(SAQER_HOOK_URL, json=body, headers=headers, timeout=6)
        if 200 <= r.status_code < 300:
            LAST_SIGNAL_TS = time.time()
            tg_send(f"🚀 BUY {coin} ({score:.2f}) — {why[:120]}")
        else:
            tg_send(f"⚠️ فشل إرسال buy لصقر: {r.status_code} {r.text[:120]}")
    except Exception as e:
        tg_send(f"🐞 send_buy err: {e}")

# ===== اختيار top1 من طبقات متعددة =====
def pick_and_emit(cache, markets):
    # 1) Gap-Sniper أولًا (فرصة لحظية)
    best_coin=None; best_score=0.0; best_why=""
    for m in markets[:min(12,len(markets))]:
        s_meta, meta = gap_sniper(m, cache)
        if s_meta>0:
            ok, reason = quality_guards(m, {"slip": estimate_slippage_pct((cache.get(("book",m)) or book(m,3))["asks"], BUY_EUR)})
            if ok and s_meta > best_score:
                best_coin, best_score = m.split("-")[0], s_meta
                best_why = f"gap:{meta.get('why','')}"
    # 2) Multi-Score عام
    for m in markets:
        s, meta = score_market(m, cache)
        ok, reason = quality_guards(m, meta)
        if not ok: continue
        s_star = adjusted_s_star(m.split("-")[0])
        if s >= s_star and s > best_score:
            best_coin, best_score = m.split("-")[0], s
            best_why = meta.get("why","")
    if best_coin:
        send_buy(best_coin, best_score, best_why)
        return True
    return False

# ===== حلقات السكان =====
def scanner_loop(run_id):
    tg_send(f"🔎 سكان جديد run={run_id}")
    cache={}
    # بناء الكون وتقسيمه: أعلى سيولة أولاً
    mkts_raw = list_markets_eur()
    # فلترة أولية حسب minQuote والرموز المعطلة
    mkts = [m for (m,b,pp,minq) in mkts_raw if minq<=50.0]  # استبعد أسواق minQuote كبيرة
    # سيولة تقريبية عبر depth snapshot (ثقيلة لو لكل السوق — نقتصر)
    def sort_by_liq(M):
        scored=[]
        for m in M:
            b = book(m,1)
            if not b: continue
            scored.append((m, (b["depth_bid_eur"]+b["depth_ask_eur"])))
        return [m for m,_ in sorted(scored, key=lambda x: x[1], reverse=True)]
    # HOT & SCOUT sets
    HOT = sort_by_liq(mkts)[:HOT_SIZE]
    SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]

    hot_t=0; scout_t=0; new_t=0
    while run_id == RUN_ID:
        now = time.time()
        # لا ترسل أكثر من إشارة كل MIN_COOLDOWN_READY_SEC
        if now - LAST_SIGNAL_TS < MIN_COOLDOWN_READY_SEC:
            time.sleep(0.2); continue
        # Hot loop كل ~1s
        if time.time() - hot_t >= 1.0:
            if pick_and_emit(cache, HOT):
                return
            hot_t = time.time()
        # Scout loop كل ~5s
        if time.time() - scout_t >= 5.0:
            if pick_and_emit(cache, SCOUT):
                return
            scout_t = time.time()
        # New listings رادار كل 60s (يبحث عن أسواق EUR جديدة ويضيفها)
        if time.time() - new_t >= MARKETS_REFRESH_SEC:
            mkts_new = [m for (m,b,pp,minq) in list_markets_eur() if m.endswith("-EUR")]
            added = [m for m in mkts_new if m not in mkts]
            if added:
                tg_send(f"🆕 أسواق جديدة: {', '.join(a.split('-')[0] for a in added[:6])} ...")
                mkts = mkts_new
                HOT = sort_by_liq(mkts)[:HOT_SIZE]
                SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]
            new_t = time.time()
        time.sleep(0.05)
    tg_send(f"⏹️ أوقفنا سكان run={run_id} (قديم)")

# ===== Flask Routes =====
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
        tg_send("✅ Scan يدوي بدأ.")
    return jsonify(ok=True)

@app.route("/ready", methods=["POST"])
def on_ready():
    global RUN_ID
    if LINK_SECRET and request.headers.get("X-Link-Secret","") != LINK_SECRET:
        return jsonify(ok=False, err="bad secret"), 401
    data = request.get_json(silent=True) or {}
    coin  = data.get("coin"); reason=data.get("reason"); pnl=data.get("pnl_eur")
    tg_send(f"📩 Ready من صقر — {coin} ({reason}) pnl={pnl}")
    # تعلم تكيفي
    if coin:
        try: learn_update(coin, float(pnl or 0.0), str(reason or ""))
        except: pass
    if reason in ("buy_failed","taker_failed"):
        # كول داون على العملة
        COOLDOWN_UNTIL[coin] = time.time() + MIN_COOLDOWN_FAIL_MIN*60
    if AUTOSCAN_ON_READY:
        RUN_ID += 1
        threading.Thread(target=scanner_loop, args=(RUN_ID,), daemon=True).start()
    return jsonify(ok=True)

@app.route("/", methods=["GET"])
def home():
    return f"Abosiyah Pro v3 ✅ run={RUN_ID} | learn={len(LEARN)} | cooldown={len(COOLDOWN_UNTIL)}", 200

# ===== Main =====
if __name__=="__main__":
    port = int(os.getenv("PORT","8082"))
    tg_send("🚀 Abosiyah Pro v3 started.")
    app.run("0.0.0.0", port, threaded=True)