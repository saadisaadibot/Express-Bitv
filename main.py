# -*- coding: utf-8 -*-
"""
Abosiyah Pro v3 — 15m Profit Hunter (full, with error alerts & trades fallback)

ENV (examples):
  BOT_TOKEN, CHAT_ID
  SAQEAR_HOOK_URL="https://your-saqar-host/hook", LINK_SECRET=""
  AUTOSCAN_ON_READY=1
  SCORE_STAR=0.65
  MAX_SPREAD=0.22
  MAX_SLIP=0.15
  DEPTH_MIN_EUR=3000
  TTL_SEC=60
  TP_EUR_HINT=0.06
  MIN_COOLDOWN_READY_SEC=30
  MIN_COOLDOWN_FAIL_MIN=30
  BUY_EUR=25
  MARKETS_REFRESH_SEC=60
  HOT_SIZE=18
  SCOUT_SIZE=60
  ERROR_COOLDOWN_SEC=60
"""

import os, time, threading, requests
from flask import Flask, request, jsonify

# ===== إعدادات عامة =====
BITVAVO = "https://api.bitvavo.com/v2"
BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
SAQAR_HOOK_URL = os.getenv("SAQAR_HOOK_URL","").strip()
LINK_SECRET = os.getenv("LINK_SECRET","").strip()

AUTOSCAN_ON_READY = int(os.getenv("AUTOSCAN_ON_READY","1"))
SCORE_STAR  = float(os.getenv("SCORE_STAR","0.65"))
MAX_SPREAD  = float(os.getenv("MAX_SPREAD","0.22"))          # %
MAX_SLIP    = float(os.getenv("MAX_SLIP","0.15"))            # %
DEPTH_MIN_EUR = float(os.getenv("DEPTH_MIN_EUR","3000"))
TTL_SEC     = int(os.getenv("TTL_SEC","60"))
TP_EUR_HINT = float(os.getenv("TP_EUR_HINT","0.06"))
MIN_COOLDOWN_READY_SEC = int(os.getenv("MIN_COOLDOWN_READY_SEC","30"))
MIN_COOLDOWN_FAIL_MIN  = int(os.getenv("MIN_COOLDOWN_FAIL_MIN","30"))
BUY_EUR     = float(os.getenv("BUY_EUR","25"))
MARKETS_REFRESH_SEC = int(os.getenv("MARKETS_REFRESH_SEC","60"))
HOT_SIZE    = int(os.getenv("HOT_SIZE","18"))
SCOUT_SIZE  = int(os.getenv("SCOUT_SIZE","60"))
ERROR_COOLDOWN_SEC = int(os.getenv("ERROR_COOLDOWN_SEC","60"))

# ===== حالة داخلية =====
RUN_ID = 0
COOLDOWN_UNTIL = {}    # coin -> ts
LEARN = {}             # coin -> {"pnl_ema":..., "win_ema":..., "adj":...}
LAST_SIGNAL_TS = 0
_LAST_ERR = {}         # error-key -> ts
TRADES_BAN_UNTIL = {}  # market -> ts (ban trades after 403)

app = Flask(__name__)

# ===== Telegram =====
def tg_send(txt: str):
    if not BOT_TOKEN: 
        print("TG:", txt); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": txt},
            timeout=6
        )
    except Exception as e:
        print("tg_send err:", e)

# ===== تنبيهات أخطاء (مع مكبح تكرار) =====
def _should_report(key: str) -> bool:
    ts = _LAST_ERR.get(key, 0)
    if time.time() - ts >= ERROR_COOLDOWN_SEC:
        _LAST_ERR[key] = time.time()
        return True
    return False

def report_error(tag: str, detail: str):
    key = f"{tag}:{detail[:60]}"
    if _should_report(key):
        tg_send(f"🛑 {tag} — {detail}")

def _url_ok(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))

if not _url_ok(SAQAR_HOOK_URL):
    report_error("config", "SAQAR_HOOK_URL غير صالح أو فارغ — لن أرسل buy حتى يُضبط.")

# ===== Bitvavo helpers (قراءة عامة) =====
def bv_safe(path, timeout=6, params=None, tag=None):
    tag = tag or path
    try:
        r = requests.get(f"{BITVAVO}{path}", params=params, timeout=timeout)
        if not (200 <= r.status_code < 300):
            report_error(f"API error {tag}", f"HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except Exception as e:
            report_error(f"JSON error {tag}", str(e))
            return None
    except requests.Timeout:
        report_error(f"Timeout {tag}", f"after {timeout}s")
        return None
    except Exception as e:
        report_error(f"HTTP exc {tag}", f"{type(e).__name__}: {e}")
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
            continue
    return out

def book(market, depth=3):
    data = bv_safe(f"/{market}/book", params={"depth": depth}, tag=f"/book {market}")
    if not isinstance(data, dict):
        return None
    try:
        bids=[]; asks=[]
        for row in data.get("bids") or []:
            if len(row)>=2: bids.append((float(row[0]), float(row[1])))
        for row in data.get("asks") or []:
            if len(row)>=2: asks.append((float(row[0]), float(row[1])))
        if not bids or not asks:
            report_error("bad book", f"{market} (no bid/ask)")
            return None
        best_bid = bids[0][0]; best_ask = asks[0][0]
        spread = (best_ask-best_bid)/best_bid*100 if best_bid>0 else 9e9
        depth_bid = sum(p*a for p,a in bids[:3])
        depth_ask = sum(p*a for p,a in asks[:3])
        return {
            "bid":best_bid,"ask":best_ask,"spread_pct":spread,
            "depth_bid_eur":depth_bid,"depth_ask_eur":depth_ask,
            "bids":bids,"asks":asks
        }
    except Exception as e:
        report_error("parse /book", f"{market} {type(e).__name__}: {e}")
        return None

def candles(market, interval="1m", limit=240):
    data = bv_safe(f"/{market}/candles", params={"interval":interval,"limit":limit}, tag=f"/candles {market}")
    return data if isinstance(data, list) else []

def trades(market, limit=60):
    # منع مؤقت إن كان السوق محظور trades (403) مؤخراً
    if TRADES_BAN_UNTIL.get(market, 0) > time.time():
        return []
    try:
        r = requests.get(f"{BITVAVO}/trades", params={"market": market, "limit": limit}, timeout=6)
        if r.status_code == 403:
            TRADES_BAN_UNTIL[market] = time.time() + 600  # 10 دقائق
            report_error("trades 403", f"{market} — fallback إلى الشموع لمدة 10د")
            return []
        if not (200 <= r.status_code < 300):
            report_error("API error /trades", f"{market} — HTTP {r.status_code}")
            return []
        try:
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            report_error("JSON /trades", f"{market} — {e}")
            return []
    except requests.Timeout:
        report_error("Timeout /trades", f"{market} بعد 6s")
        return []
    except Exception as e:
        report_error("HTTP exc /trades", f"{market} — {type(e).__name__}: {e}")
        return []

# ===== تقديرات سريعة =====
def estimate_slippage_pct(asks, want_eur: float):
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

def tape_from_candles_fallback(market):
    """
    (uptick_proxy, speed_proxy):
    - uptick_proxy: نسبة شموع خضراء مرجّحة بالحجوم آخر 5 دقائق (0..1)
    - speed_proxy : معدل الحركة الأخيرة (٪/دقيقة) → نعيده كنطاق 0..2 مثل trades_10s_speed
    """
    cs = candles(market, "1m", limit=6)
    if not cs or len(cs) < 4:
        return 0.5, 0.0
    closes = [float(r[4]) for r in cs]
    opens  = [float(r[1]) for r in cs]
    vols   = [float(r[5]) for r in cs]
    greens = 0.0; vol_sum = 0.0
    for o,c,v in zip(opens[-6:-1], closes[-6:-1], vols[-6:-1]):
        w = max(v, 1e-9)
        vol_sum += w
        if c >= o: greens += w
    uptick_proxy = (greens / max(vol_sum, 1e-9)) if vol_sum>0 else 0.5
    r1 = (closes[-1] / max(closes[-2], 1e-9) - 1.0) * 100.0
    r2 = (closes[-2] / max(closes[-3], 1e-9) - 1.0) * 100.0
    speed_proxy = max(-2.0, min(2.0, 0.6*r1 + 0.4*r2))
    speed_norm = max(0.0, min(2.0, abs(speed_proxy)))
    return float(uptick_proxy), float(speed_norm)

def uptick_ratio(trs):
    if not trs: return 0.0
    upt = sum(1 for t in trs if (t.get("side","").lower()=="buy"))
    return upt / max(1, len(trs))

def trades_10s_speed(trs, now_ms):
    recent = [t for t in trs if (now_ms - int(t.get("timestamp",0) or 0)) <= 10_000]
    return len(recent) / 10.0

def vwap5m(market):
    cs = candles(market,"5m", limit=1)
    try: return float(cs[-1][4])
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
    except: return 0.0, 50.0
    if len(closes)<60: return 0.0, 50.0
    gains=loss=0.0
    for i in range(-14,0):
        d = closes[i]-closes[i-1]
        gains += d if d>=0 else 0.0
        loss  += -d if d<0 else 0.0
    avg_gain = gains/14
    avg_loss = loss/14 if loss>0 else 1e-9
    rs = avg_gain/avg_loss
    rsi = 100.0 - (100.0 / (1.0+rs))
    trs=[]
    for i in range(1,len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    if len(trs)<20: return 0.0, rsi
    atr = sum(trs[-14:])/14
    adx = min(40.0, max(0.0, (atr/max(1e-9, closes[-1]))*1000*1.2))
    return adx, rsi

# ===== Multi-Score =====
def score_market(market, cache):
    now_ms = int(time.time()*1000)
    bk = cache.get(("book",market))
    if bk is None:
        bk = book(market,3); cache[("book",market)] = bk
    trs = cache.get(("trades",market))
    if trs is None or (trs and now_ms - int(trs[0].get("timestamp",0) or 0) > 2000):
        trs = trades(market, 60); cache[("trades",market)] = trs

    if not bk or bk["bid"]<=0 or bk["ask"]<=0:
        return 0.0, {"why":"no_book"}

    spread = bk["spread_pct"]
    depthB = bk["depth_bid_eur"]; depthA = bk["depth_ask_eur"]

    # Tape: trades أو fallback من الشموع
    if trs:
        ur = uptick_ratio(trs)
        spd = trades_10s_speed(trs, now_ms)
    else:
        ur, spd = tape_from_candles_fallback(market)

    last, bo15 = h15_breakout(market)
    vw = vwap5m(market)
    above_vwap = 1.0 if (vw>0 and last>=vw) else 0.0
    adx, rsi = adx_rsi_lite(market)
    slip = estimate_slippage_pct(bk["asks"], BUY_EUR)

    z_tape = min(1.0, 0.5*ur + 0.5*min(1.0, spd/2.0))
    imb = depthB / max(1.0, (depthA+depthB))
    z_imb  = max(0.0, (imb - 0.45)/0.25)
    z_break= max(0.0, min(1.0, (bo15/0.5))) * 0.7 + above_vwap*0.3
    z_vol  = min(1.0, adx/30.0)
    z_reg  = 1.0 if (55<=rsi<=75) else (0.6 if 50<=rsi<55 or 75<rsi<=80 else 0.2)

    score = 0.35*z_tape + 0.25*z_imb + 0.20*z_break + 0.10*z_vol + 0.10*z_reg
    why = (
        f"ur={ur:.2f},spd={spd:.2f},imb={imb:.2f},bo15={bo15:.2f}%,"
        f"adx~{adx:.1f},rsi={rsi:.0f},spr={spread:.2f}%,slip~{slip:.2f}%"
        + ("" if trs else " [fallback]")
    )
    meta = {"why": why, "spread": spread, "slip": slip, "imb": imb, "ur": ur, "spd": spd}
    return score, meta

# ===== Gap-Sniper =====
def gap_sniper(market, cache):
    bk = cache.get(("book",market))
    if bk is None:
        bk = book(market,3); cache[("book",market)] = bk
    if not bk: return 0.0, {}
    depthA = bk["depth_ask_eur"]; depthB = bk["depth_bid_eur"]
    if depthA<=0 or depthB<=0: return 0.0, {}
    ratio = depthB / max(1e-9, depthA)
    spr = bk["spread_pct"]
    score = 0.0
    if spr <= MAX_SPREAD and ratio >= 2.0:
        score = min(1.0, (ratio-2.0)/3.0 + 0.5)
    return score, {"why": f"gap ratio={ratio:.2f}, spr={spr:.2f}%"}

# ===== قاردات الجودة =====
def quality_guards(market, meta):
    bk = book(market,3)
    if not bk: return False, "no_book"
    if bk["spread_pct"] > MAX_SPREAD: return False, "spread"
    if bk["depth_ask_eur"] < DEPTH_MIN_EUR: return False, "depth"
    slip_pct = meta.get("slip", 9e9)
    if slip_pct > MAX_SLIP: return False, "slip"
    coin = market.split("-")[0]
    if COOLDOWN_UNTIL.get(coin,0) > time.time(): return False, "cooldown"
    return True, "ok"

# ===== تعلم بسيط لكل عملة =====
def learn_update(coin, pnl_eur, reason):
    L = LEARN.get(coin, {"pnl_ema":0.0,"win_ema":0.5,"adj":0.0})
    alpha=0.3
    L["pnl_ema"] = (1-alpha)*L["pnl_ema"] + alpha*(pnl_eur or 0.0)
    if reason in ("tp_filled", "manual_sell_filled"):
        L["win_ema"] = (1-alpha)*L["win_ema"] + alpha*1.0
    else:
        L["win_ema"] = (1-alpha)*L["win_ema"] + alpha*0.0
    L["adj"] = max(-0.05, min(0.08, 0.08*(0.5 - L["win_ema"])))
    LEARN[coin]=L

def adjusted_s_star(coin):
    adj = (LEARN.get(coin) or {}).get("adj", 0.0)
    return max(0.52, min(0.90, SCORE_STAR + adj))

# ===== إرسال الإشارة لصقر (مع رصد أخطاء) =====
def send_buy(coin, score, why):
    global LAST_SIGNAL_TS
    if not _url_ok(SAQAR_HOOK_URL):
        report_error("send_buy", "SAQAR_HOOK_URL مفقود/غير صالح — تجاهلت الإرسال.")
        return
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
        r = requests.post(SAQAR_HOOK_URL, json=body, headers=headers, timeout=6)
        if 200 <= r.status_code < 300:
            LAST_SIGNAL_TS = time.time()
            tg_send(f"🚀 BUY {coin} ({score:.2f}) — {why[:120]}")
        else:
            report_error("send_buy", f"HTTP {r.status_code} {r.text[:120]}")
    except requests.Timeout:
        report_error("send_buy", "timeout 6s")
    except Exception as e:
        report_error("send_buy", f"{type(e).__name__}: {e}")

# ===== اختيار top1 من طبقات متعددة =====
def pick_and_emit(cache, markets):
    best_coin=None; best_score=0.0; best_why=""
    # 1) Gap-Sniper
    for m in markets[:min(12,len(markets))]:
        s_meta, meta = gap_sniper(m, cache)
        if s_meta>0:
            ok, _ = quality_guards(m, {"slip": estimate_slippage_pct((cache.get(("book",m)) or book(m,3))["asks"], BUY_EUR)})
            if ok and s_meta > best_score:
                best_coin, best_score = m.split("-")[0], s_meta
                best_why = f"gap:{meta.get('why','')}"
    # 2) Multi-Score
    for m in markets:
        s, meta = score_market(m, cache)
        ok, _ = quality_guards(m, meta)
        if not ok: continue
        s_star = adjusted_s_star(m.split("-")[0])
        if s >= s_star and s > best_score:
            best_coin, best_score = m.split("-")[0], s
            best_why = meta.get("why","")
    if best_coin:
        send_buy(best_coin, best_score, best_why)
        return True
    return False

# ===== ترتيب السيولة =====
def sort_by_liq(M):
    scored=[]
    for m in M:
        b = book(m,1)
        if not b: continue
        scored.append((m, (b["depth_bid_eur"]+b["depth_ask_eur"])))
    return [m for m,_ in sorted(scored, key=lambda x: x[1], reverse=True)]

# ===== حلقة السكانر =====
def scanner_loop(run_id):
    tg_send(f"🔎 سكان جديد run={run_id}")
    cache={}
    try:
        mkts_raw = list_markets_eur()
        if not mkts_raw:
            report_error("scanner", "no markets returned")
        mkts = [m for (m,b,pp,minq) in mkts_raw if m.endswith("-EUR") and minq<=50.0]
        HOT = sort_by_liq(mkts)[:HOT_SIZE]
        SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]

        hot_t=scout_t=new_t=0
        while run_id == RUN_ID:
            now = time.time()
            if now - LAST_SIGNAL_TS < MIN_COOLDOWN_READY_SEC:
                time.sleep(0.2); continue

            try:
                if time.time() - hot_t >= 1.0:
                    if pick_and_emit(cache, HOT):
                        return
                    hot_t = time.time()
            except Exception as e:
                report_error("hot loop", f"{type(e).__name__}: {e}")

            try:
                if time.time() - scout_t >= 5.0:
                    if pick_and_emit(cache, SCOUT):
                        return
                    scout_t = time.time()
            except Exception as e:
                report_error("scout loop", f"{type(e).__name__}: {e}")

            try:
                if time.time() - new_t >= MARKETS_REFRESH_SEC:
                    mkts_new = [m for (m,b,pp,minq) in list_markets_eur() if m.endswith("-EUR")]
                    added = [m for m in mkts_new if m not in mkts]
                    if added:
                        tg_send(f"🆕 أسواق جديدة: {', '.join(a.split('-')[0] for a in added[:6])} ...")
                        mkts = mkts_new
                        HOT = sort_by_liq(mkts)[:HOT_SIZE]
                        SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]
                    new_t = time.time()
            except Exception as e:
                report_error("refresh markets", f"{type(e).__name__}: {e}")

            time.sleep(0.05)

        tg_send(f"⏹️ run={run_id} stopped (superseded by run={RUN_ID})")
    except Exception as e:
        report_error("scanner crashed", f"{type(e).__name__}: {e}")

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
    if coin:
        try: learn_update(coin, float(pnl or 0.0), str(reason or ""))
        except: pass
    if reason in ("buy_failed","taker_failed"):
        COOLDOWN_UNTIL[coin] = time.time() + MIN_COOLDOWN_FAIL_MIN*60
    if AUTOSCAN_ON_READY:
        RUN_ID += 1
        threading.Thread(target=scanner_loop, args=(RUN_ID,), daemon=True).start()
    return jsonify(ok=True)

@app.route("/health", methods=["GET"])
def health():
    return jsonify(ok=True, run_id=RUN_ID, last_signal_ts=LAST_SIGNAL_TS,
                   learn=len(LEARN), cooldown=len(COOLDOWN_UNTIL)), 200

@app.route("/", methods=["GET"])
def home():
    return f"Abosiyah Pro v3 ✅ run={RUN_ID} | learn={len(LEARN)} | cooldown={len(COOLDOWN_UNTIL)}", 200

# ===== Main =====
if __name__=="__main__":
    port = int(os.getenv("PORT","8082"))
    tg_send("🚀 Abosiyah Pro v3 started.")
    app.run("0.0.0.0", port, threaded=True)