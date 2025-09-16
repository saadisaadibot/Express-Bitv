# -*- coding: utf-8 -*-
"""
Express Pro v4 — Lightning Momentum Hunter (saqar-style webhook)
- سرعة عالية + حدس بسيط: Tape/Speed + Breakout + Orderbook Gap (ثلاث محرّكات).
- حُرّاس جودة خفاف (سبريد، سيولة معروضة، انزلاق تقديري).
- وضع هجومي يقلل الفلاتر ويحاول عدم تفويت الرّنّانات (ON افتراضياً).
- تفادي رسائل 403/SSL سبامية + مبرّد للأخطاء.
- /scan يدوي، autoscan عند /ready، /health.
- رسائل Telegram مُركّزة مع تقييم ألوان (🟢🟡🔴) حسب الجودة.

ENV (أمثلة):
  BOT_TOKEN, CHAT_ID
  SAQAR_WEBHOOK="http://saqar:8080"        # لا تضع /hook
  AUTOSCAN_ON_READY=1
  AGGRESSIVE=1                              # 1=هجومي، 0=تحفّظ
  BUY_EUR=25
  MAX_SPREAD=0.30                           # % أقصى سبريد
  DEPTH_MIN_EUR=2000                        # حد أدنى عمق asks
  MAX_SLIP=0.25                             # % انزلاق تقديري
  SCORE_STAR=0.60                           # عتبة multi-score بالوضع التحفّظي
  TTL_SEC=60
  MARKETS_REFRESH_SEC=45
  HOT_SIZE=20
  SCOUT_SIZE=80
  ERROR_COOLDOWN_SEC=60
  MIN_COOLDOWN_READY_SEC=30
  MIN_COOLDOWN_FAIL_MIN=30
"""

import os, time, threading, requests, math
from flask import Flask, request, jsonify

# ===== إعدادات عامة =====
BITVAVO = "https://api.bitvavo.com/v2"
BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = os.getenv("CHAT_ID","").strip()
SAQAR_URL   = os.getenv("SAQAR_WEBHOOK","").strip().rstrip("/")

AUTOSCAN_ON_READY = int(os.getenv("AUTOSCAN_ON_READY","1"))
AGGR              = int(os.getenv("AGGRESSIVE","1"))

BUY_EUR   = float(os.getenv("BUY_EUR","25"))
MAX_SPREAD= float(os.getenv("MAX_SPREAD","0.30"))
DEPTH_MIN = float(os.getenv("DEPTH_MIN_EUR","2000"))
MAX_SLIP  = float(os.getenv("MAX_SLIP","0.25"))
SCORE_STAR= float(os.getenv("SCORE_STAR","0.60"))
TTL_SEC   = int(os.getenv("TTL_SEC","60"))

MARKETS_REFRESH_SEC = int(os.getenv("MARKETS_REFRESH_SEC","45"))
HOT_SIZE    = int(os.getenv("HOT_SIZE","20"))
SCOUT_SIZE  = int(os.getenv("SCOUT_SIZE","80"))

ERROR_COOLDOWN_SEC = int(os.getenv("ERROR_COOLDOWN_SEC","60"))
MIN_COOLDOWN_READY_SEC = int(os.getenv("MIN_COOLDOWN_READY_SEC","30"))
MIN_COOLDOWN_FAIL_MIN  = int(os.getenv("MIN_COOLDOWN_FAIL_MIN","30"))

# ===== حالة داخلية =====
RUN_ID = 0
COOLDOWN_UNTIL = {}           # coin -> ts
LEARN = {}                    # coin -> {win_ema,pnl_ema,adj}
LAST_SIGNAL_TS = 0
_LAST_ERR = {}                # key->ts
TRADES_BAN_UNTIL = {}         # market->ts (403 ban)
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

# ===== مكبح أخطاء (ومنها 403/SSL) =====
def _should_report(key: str) -> bool:
    ts = _LAST_ERR.get(key, 0)
    if time.time() - ts >= ERROR_COOLDOWN_SEC:
        _LAST_ERR[key] = time.time(); return True
    return False

def report_error(tag: str, detail: str):
    key = f"{tag}:{detail[:64]}"
    if _should_report(key):
        tg_send(f"🛑 {tag} — {detail}")

def _url_ok(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("http://","https://"))

if not _url_ok(SAQAR_URL):
    report_error("config","SAQAR_WEBHOOK غير مضبوط — لن تُرسل إشارات.")

# ===== Bitvavo (قراءات عامة) =====
def bv_safe(path, timeout=6, params=None, tag=None):
    tag = tag or path
    try:
        r = requests.get(f"{BITVAVO}{path}", params=params, timeout=timeout)
        if not (200 <= r.status_code < 300):
            # كتم 403 من /trades (يتعالج في trades)
            if not (path.endswith("/trades") and r.status_code==403):
                report_error(f"API {tag}", f"HTTP {r.status_code}")
            return None
        try: return r.json()
        except Exception as e:
            report_error(f"JSON {tag}", str(e)); return None
    except requests.Timeout:
        report_error(f"Timeout {tag}", f"after {timeout}s"); return None
    except Exception as e:
        # SSL EOF وغيره
        report_error(f"HTTP exc {tag}", f"{type(e).__name__}: {e}"); return None

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

def book(market, depth=3):
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
        depth_bid = sum(p*a for p,a in bids[:3])
        depth_ask = sum(p*a for p,a in asks[:3])
        return {
            "bid":best_bid,"ask":best_ask,"spread_pct":spread,
            "depth_bid_eur":depth_bid,"depth_ask_eur":depth_ask,
            "bids":bids,"asks":asks
        }
    except Exception as e:
        report_error("parse /book", f"{market} {type(e).__name__}: {e}"); return None

def candles(market, interval="1m", limit=240):
    data = bv_safe(f"/{market}/candles", params={"interval":interval,"limit":limit}, tag=f"/candles {market}")
    return data if isinstance(data, list) else []

def trades(market, limit=60):
    # /trades أحياناً 403 — نطبّق ban 10 د لكل ماركت ثم نستخدم fallback
    if TRADES_BAN_UNTIL.get(market, 0) > time.time(): return []
    try:
        r = requests.get(f"{BITVAVO}/trades", params={"market": market, "limit": limit}, timeout=5)
        if r.status_code == 403:
            TRADES_BAN_UNTIL[market] = time.time() + 600
            # لا ترسل رسالة هنا (مكبوت)
            return []
        if not (200 <= r.status_code < 300): return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

# ===== تقديرات خفيفة =====
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

def uptick_ratio(trs):
    if not trs: return 0.0
    buys = sum(1 for t in trs if (t.get("side","").lower())=="buy")
    return buys / max(1, len(trs))

def trades_10s_speed(trs, now_ms):
    recent = [t for t in trs if (now_ms - int(t.get("timestamp",0) or 0)) <= 10_000]
    return len(recent) / 10.0

def tape_from_candles_fallback(market):
    cs = candles(market,"1m", limit=6)
    if not cs or len(cs)<4: return 0.5, 0.0
    closes=[float(r[4]) for r in cs]
    opens =[float(r[1]) for r in cs]
    vols  =[float(r[5]) for r in cs]
    greens=0.0; volsum=0.0
    for o,c,v in zip(opens[-6:-1], closes[-6:-1], vols[-6:-1]):
        w=max(v,1e-9); volsum += w
        if c>=o: greens+=w
    ur = (greens/max(volsum,1e-9)) if volsum>0 else 0.5
    r1=(closes[-1]/max(closes[-2],1e-9)-1)*100.0
    r2=(closes[-2]/max(closes[-3],1e-9)-1)*100.0
    spd=max(-2.5,min(2.5,0.65*r1+0.35*r2))
    return float(ur), float(abs(spd))

def vwap5m(market):
    cs = candles(market, "5m", 1)
    try: return float(cs[-1][4])
    except: return 0.0

def h15_breakout(market):
    cs = candles(market,"1m", limit=16)
    try:
        highs=[float(r[2]) for r in cs[:-1]]
        last=float(cs[-1][4])
        return last, (last - max(highs))/max(1e-12, max(highs))*100.0
    except: return 0.0, 0.0

def adx_rsi_lite(market):
    cs = candles(market,"1m", limit=120)
    try:
        closes=[float(r[4]) for r in cs]
        highs =[float(r[2]) for r in cs]
        lows  =[float(r[3]) for r in cs]
    except: return 0.0, 50.0
    if len(closes)<40: return 0.0, 50.0
    gains=loss=0.0
    for i in range(-14,0):
        d=closes[i]-closes[i-1]
        gains += d if d>=0 else 0.0
        loss  += -d if d<0 else 0.0
    rs = (gains/14)/max(loss/14,1e-9)
    rsi = 100.0 - (100.0/(1.0+rs))
    # ATR نسبية كبديل ADX
    trs=[]
    for i in range(1,len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr = sum(trs[-14:])/max(14,1)
    adx = min(45.0, max(0.0, (atr/max(1e-9, closes[-1]))*1000))
    return adx, rsi

# ===== تقييم ألوان بسيط =====
def color_emoji(x, good, warn):
    # >=good: green, >=warn: yellow, else red
    return "🟢" if x>=good else ("🟡" if x>=warn else "🔴")

# ===== محرّك 1: Gap / orderbook dominance =====
def engine_gap(market, cache):
    bk = cache.get(("book",market))
    if bk is None:
        bk = book(market,3); cache[("book",market)] = bk
    if not bk: return 0.0, {}
    spr = bk["spread_pct"]; B=bk["depth_bid_eur"]; A=bk["depth_ask_eur"]
    if A<=0 or B<=0: return 0.0, {}
    ratio = B/max(A,1e-9)
    score = 0.0
    if spr<=MAX_SPREAD and ratio>=2.0:
        score = min(1.0, 0.45 + (ratio-2.0)/3.0)
    why = f"gap r={ratio:.2f}, spr={spr:.2f}%"
    return score, {"why": why, "spread": spr}

# ===== محرّك 2: Tape/Speed + VWAP =====
def engine_tape(market, cache):
    now_ms = int(time.time()*1000)
    trs = cache.get(("trades",market))
    if trs is None:
        trs = trades(market, 60); cache[("trades",market)] = trs
    if trs:
        ur = uptick_ratio(trs); spd = trades_10s_speed(trs, now_ms)
    else:
        ur, spd = tape_from_candles_fallback(market)
    last, bo15 = h15_breakout(market)
    vw = vwap5m(market)
    above = 1.0 if (vw>0 and last>=vw) else 0.0
    adx, rsi = adx_rsi_lite(market)
    # خلي المومنتم يوزن أعلى بالوضع الهجومي
    w_tape = 0.55 if AGGR else 0.40
    z_tape = min(1.0, 0.6*ur + 0.4*min(1.0, spd/2.0))
    z_break= max(0.0, min(1.0, bo15/0.5))*0.7 + above*0.3
    z_vol  = min(1.0, adx/30.0)
    z_reg  = 1.0 if (50<=rsi<=75) else (0.6 if 45<=rsi<50 or 75<rsi<=80 else 0.2)
    score  = w_tape*z_tape + 0.25*z_break + 0.15*z_vol + 0.05*z_reg
    bk = cache.get(("book",market)) or book(market,3)
    slip = estimate_slippage_pct((bk or {}).get("asks") or [], BUY_EUR) if bk else 9e9
    spr  = (bk or {}).get("spread_pct", 9e9) if bk else 9e9
    meta = {
        "why": f"ur={ur:.2f} {color_emoji(ur,0.62,0.54)} | spd={spd:.2f} {color_emoji(spd,0.9,0.5)} | "
               f"bo15={bo15:.2f}% {color_emoji(bo15,0.25,0.10)} | "
               f"adx~{adx:.1f} {color_emoji(adx,22,14)} | rsi={rsi:.0f} | "
               f"spr={spr:.2f}% | slip~{slip:.2f}%",
        "spread": spr, "slip": slip, "ur": ur, "spd": spd
    }
    return score, meta

# ===== محرّك 3: Breakout قوي جداً (اندفاع) =====
def engine_blast(market, cache):
    cs = candles(market,"1m", limit=8)
    if not cs or len(cs)<6: return 0.0, {}
    closes=[float(r[4]) for r in cs]
    # تسارع: عوائد 1m مؤخراً
    def roc(n): 
        return (closes[-1]/max(closes[-n],1e-9)-1.0)*100.0 if len(closes)>n else 0.0
    r1=roc(2); r3=roc(4); r5=roc(6)
    boost = max(0.0, 0.8*r1 + 0.6*r3 + 0.4*r5)    # يلتقط الاندفاعات
    score = min(1.0, boost/1.2)                   # تطبيع سريع
    return score, {"why": f"blast r1={r1:.2f}% r3={r3:.2f}% r5={r5:.2f}%"}

# ===== حُرّاس خفاف =====
def quality_guards(market, meta):
    bk = book(market,3)
    if not bk: return False, "no_book"
    if bk["spread_pct"] > MAX_SPREAD: return False, "spread"
    if bk["depth_ask_eur"] < DEPTH_MIN: return False, "depth"
    if meta.get("slip",0) > MAX_SLIP: return False, "slip"
    coin = market.split("-")[0]
    if COOLDOWN_UNTIL.get(coin,0) > time.time(): return False, "cooldown"
    return True, "ok"

# ===== تعلّم بسيط يحرّك عتبة الاختيار =====
def learn_update(coin, pnl_eur, reason):
    L = LEARN.get(coin, {"pnl_ema":0.0,"win_ema":0.5,"adj":0.0})
    a=0.3
    L["pnl_ema"] = (1-a)*L["pnl_ema"] + a*(pnl_eur or 0.0)
    L["win_ema"] = (1-a)*L["win_ema"] + a*(1.0 if reason in ("tp_filled","manual_sell_filled") else 0.0)
    L["adj"]     = max(-0.06, min(0.10, 0.10*(0.5 - L["win_ema"])))
    LEARN[coin]=L

def adjusted_star(coin):
    base = 0.48 if AGGR else SCORE_STAR
    return max(0.45, min(0.90, base + (LEARN.get(coin,{}).get("adj",0.0))))

# ===== إرسال إشارة لصقر (نفس أسلوبك) =====
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
            tg_send(f"🚀 أرسلت {coin} إلى صقر — {why_line}")
        else:
            report_error("send_buy", f"HTTP {r.status_code} | {r.text[:140]}")
    except Exception as e:
        report_error("send_buy", f"{type(e).__name__}: {e}")

# ===== اختيار سريع (ثلاث محرّكات) =====
def pick_and_emit(cache, markets):
    best=None
    for m in markets:
        # محرّكات
        s1, meta1 = engine_blast(m, cache)    # يلتقط الانفجارات
        s2, meta2 = engine_tape(m, cache)     # زخم مستدام + VWAP
        s3, meta3 = engine_gap(m, cache)      # تفوّق دفتر الطلبات
        # تجميع بسيط — نعطي وزن أعلى للانفجار بالهجومي
        w_blast = 0.45 if AGGR else 0.30
        w_tape  = 0.40 if AGGR else 0.45
        w_gap   = 0.15
        score   = w_blast*s1 + w_tape*s2 + w_gap*s3

        # حُرّاس خفاف
        meta = {**meta2, "spread": max(meta1.get("spread",meta2.get("spread",9e9)), meta3.get("spread",0))}
        ok,_ = quality_guards(m, meta)
        if not ok: continue

        coin=m.split("-")[0]
        star = adjusted_star(coin)
        if score >= star:
            # توليف Why مختصر + ألوان
            why = f"{meta2['why']} | {meta3.get('why','')} | ⭐{score:.2f}/{star:.2f}"
            # اختر الأعلى خلال الجولة
            if not best or score > best[0]:
                best=(score, coin, why)

        # بالهجومي: لو gap قوي جداً أو blast > 0.9 خذها فوراً
        if AGGR and (s3>=0.92 or s1>=0.95):
            coin=m.split("-")[0]
            send_buy(coin, f"fast {('gap' if s3>=0.92 else 'blast')} ⭐{max(s3,s1):.2f}")
            return True
    if best:
        send_buy(best[1], best[2]); return True
    return False

# ===== ترتيب بسيط حسب سيولة دفتر الأوامر =====
def sort_by_liq(markets):
    scored=[]
    for m in markets:
        b = book(m,1)
        if not b: continue
        scored.append((m, b["depth_bid_eur"]+b["depth_ask_eur"]))
    scored.sort(key=lambda x:x[1], reverse=True)
    return [m for m,_ in scored]

# ===== حلقة السكانر =====
def scanner_loop(run_id):
    tg_send(f"🔎 سكان جديد run={run_id}")
    cache={}
    try:
        mkts_raw = list_markets_eur()
        mkts = [m for (m,b,pp,minq) in mkts_raw if m.endswith("-EUR") and minq<=50.0]
        HOT   = sort_by_liq(mkts)[:HOT_SIZE]
        SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]
        hot_t=scout_t=refresh_t=0
        while run_id == RUN_ID:
            if time.time() - LAST_SIGNAL_TS < MIN_COOLDOWN_READY_SEC:
                time.sleep(0.15); continue
            try:
                if time.time()-hot_t >= 1.0:
                    if pick_and_emit(cache, HOT): return
                    hot_t=time.time()
            except Exception as e:
                report_error("hot loop", f"{type(e).__name__}: {e}")
            try:
                if time.time()-scout_t >= (2.5 if AGGR else 4.0):
                    if pick_and_emit(cache, SCOUT): return
                    scout_t=time.time()
            except Exception as e:
                report_error("scout loop", f"{type(e).__name__}: {e}")
            try:
                if time.time()-refresh_t >= MARKETS_REFRESH_SEC:
                    mkts_new = [m for (m,b,pp,minq) in list_markets_eur() if m.endswith("-EUR")]
                    added = [m for m in mkts_new if m not in mkts]
                    if added:
                        tg_send("🆕 أسواق: " + ", ".join(a.split("-")[0] for a in added[:8]))
                        mkts = mkts_new
                        HOT   = sort_by_liq(mkts)[:HOT_SIZE]
                        SCOUT = sort_by_liq(mkts)[:SCOUT_SIZE]
                    refresh_t=time.time()
            except Exception as e:
                report_error("refresh", f"{type(e).__name__}: {e}")
            time.sleep(0.03 if AGGR else 0.06)
        tg_send(f"⏹️ run={run_id} stopped (superseded by run={RUN_ID})")
    except Exception as e:
        report_error("scanner crash", f"{type(e).__name__}: {e}")

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
        tg_send("✅ Scan بدأ يدويًا.")
    return jsonify(ok=True)

@app.route("/ready", methods=["POST"])
def on_ready():
    global RUN_ID
    data = request.get_json(silent=True) or {}
    coin  = (data.get("coin") or "").upper()
    reason= data.get("reason"); pnl=data.get("pnl_eur")
    tg_send(f"📩 Ready من صقر — {coin} ({reason}) pnl={pnl}")
    if coin:
        try:
            learn_update(coin, float(pnl or 0.0), str(reason))
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
                   learn=len(LEARN), cooldown=len(COOLDOWN_UNTIL), aggr=AGGR), 200

@app.route("/", methods=["GET"])
def home():
    return f"Express Pro v4 ✅ run={RUN_ID} | aggr={AGGR} | learn={len(LEARN)}", 200

# ===== Main =====
if __name__=="__main__":
    port = int(os.getenv("PORT","8082"))
    tg_send("⚡️ Express Pro v4 — started.")
    app.run("0.0.0.0", port, threaded=True)