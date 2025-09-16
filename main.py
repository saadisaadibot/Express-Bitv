# -*- coding: utf-8 -*-
"""
Express Pro v5 — Lightning Momentum Hunter (saqar-style webhook)
- نبض حي Pulse (trades speed + 1m volatility + RVOL 5m/20m)
- اتجاه خفيف: EMA9>EMA20 + فوق VWAP5m
- HH-60m Position: تفضيل الثلث العلوي
- Penalized slippage/spread بدل رمي الفرصة مبكراً
- تعلم بسيط per-coin لتحريك العتبة
- تبريد ديناميكي لعملات dull

ENV (أمثلة):
  BOT_TOKEN, CHAT_ID
  SAQAR_WEBHOOK="http://saqar:8080"        # لا تضع /hook
  AUTOSCAN_ON_READY=1
  AGGRESSIVE=1                              # 1=هجومي، 0=تحفّظ
  BUY_EUR=25
  MAX_SPREAD=0.30                           # % أقصى سبريد (حارس أخير)
  DEPTH_MIN_EUR=1800                        # حد أدنى عمق asks
  MAX_SLIP=0.35                             # % انزلاق تقديري (حارس أخير)
  SCORE_STAR=0.62                           # عتبة بالتحفّظي (الهجومي أقل)
  MARKETS_REFRESH_SEC=45
  HOT_SIZE=20
  SCOUT_SIZE=80
  ERROR_COOLDOWN_SEC=60
  MIN_COOLDOWN_READY_SEC=25
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
DEPTH_MIN = float(os.getenv("DEPTH_MIN_EUR","1800"))
MAX_SLIP  = float(os.getenv("MAX_SLIP","0.35"))
SCORE_STAR= float(os.getenv("SCORE_STAR","0.62"))

MARKETS_REFRESH_SEC = int(os.getenv("MARKETS_REFRESH_SEC","45"))
HOT_SIZE    = int(os.getenv("HOT_SIZE","20"))
SCOUT_SIZE  = int(os.getenv("SCOUT_SIZE","80"))

ERROR_COOLDOWN_SEC = int(os.getenv("ERROR_COOLDOWN_SEC","60"))
MIN_COOLDOWN_READY_SEC = int(os.getenv("MIN_COOLDOWN_READY_SEC","25"))
MIN_COOLDOWN_FAIL_MIN  = int(os.getenv("MIN_COOLDOWN_FAIL_MIN","30"))

# ===== حالة داخلية =====
RUN_ID = 0
COOLDOWN_UNTIL = {}           # coin -> ts (فشل شراء/دول)
LEARN = {}                    # coin -> {win_ema,pnl_ema,adj}
DULL_FAILS = {}               # coin -> count within window
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

# ===== مكبح أخطاء =====
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

# ===== Bitvavo (قراءات) =====
def bv_safe(path, timeout=6, params=None, tag=None):
    tag = tag or path
    try:
        r = requests.get(f"{BITVAVO}{path}", params=params, timeout=timeout)
        if not (200 <= r.status_code < 300):
            if not (path.endswith("/trades") and r.status_code==403):
                report_error(f"API {tag}", f"HTTP {r.status_code}")
            return None
        try: return r.json()
        except Exception as e:
            report_error(f"JSON {tag}", str(e)); return None
    except requests.Timeout:
        report_error(f"Timeout {tag}", f"after {timeout}s"); return None
    except Exception as e:
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

def trades(market, limit=80):
    if TRADES_BAN_UNTIL.get(market, 0) > time.time(): return []
    try:
        r = requests.get(f"{BITVAVO}/trades", params={"market": market, "limit": limit}, timeout=5)
        if r.status_code == 403:
            TRADES_BAN_UNTIL[market] = time.time() + 600
            return []
        if not (200 <= r.status_code < 300): return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

# ===== تقديرات =====
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
    if not trs: return 0.5
    buys = sum(1 for t in trs if (t.get("side","").lower())=="buy")
    return buys / max(1, len(trs))

def trades_10s_speed(trs, now_ms):
    recent = [t for t in trs if (now_ms - int(t.get("timestamp",0) or 0)) <= 10_000]
    return len(recent) / 10.0

# ===== مؤشرات خفيفة =====
def ema(vals, n):
    if not vals: return 0.0
    k=2/(n+1.0); e=vals[0]
    for v in vals[1:]: e = v*k + e*(1-k)
    return e

def rvol_5_vs_20(cs):
    # cs: 1m candles
    try:
        vols=[float(r[5]) for r in cs]
        v5=sum(vols[-5:]); v20=sum(vols[-20:])
        avg20 = v20/20.0 if v20>0 else 0.0
        return (v5/5.0)/max(avg20,1e-9)
    except: return 0.0

def hh_pos_60m(cs):
    # موضع الإغلاق ضمن أعلى/أدنى 60 دقيقة
    try:
        closes=[float(r[4]) for r in cs[-60:]]
        hi=max(closes); lo=min(closes); last=closes[-1]
        rng=max(hi-lo,1e-9)
        return (last-lo)/rng
    except: return 0.5

def vwap5m(market):
    cs = candles(market, "5m", 1)
    try: return float(cs[-1][4])
    except: return 0.0

# ===== تقييم ألوان =====
def color_emoji(x, good, warn, reverse=False):
    if reverse:
        return "🟢" if x<=good else ("🟡" if x<=warn else "🔴")
    return "🟢" if x>=good else ("🟡" if x>=warn else "🔴")

# ===== Pulse + Momentum Engine =====
def engine_pulse(market, cache):
    now_ms = int(time.time()*1000)
    cs1m = cache.get(("c1m",market))
    if cs1m is None:
        cs1m = candles(market,"1m", limit=120); cache[("c1m",market)] = cs1m

    trs = cache.get(("trades",market))
    if trs is None:
        trs = trades(market, 80); cache[("trades",market)] = trs

    ur = uptick_ratio(trs) if trs else 0.5
    spd = trades_10s_speed(trs, now_ms) if trs else 0.0

    # تذبذب 1m آخر 6 شمعات
    try:
        closes=[float(r[4]) for r in cs1m]
        volty = sum(abs((closes[i]/closes[i-1])-1.0) for i in range(-6,-1)) * 100.0
        ema9 = ema(closes[-30:], 9); ema20 = ema(closes[-60:],20)
        last = closes[-1]
    except:
        volty=0.0; ema9=ema20=last=0.0

    rvol = rvol_5_vs_20(cs1m)
    vwap = vwap5m(market)
    above_vwap = 1.0 if (vwap>0 and last>=vwap) else 0.0
    align = 1.0 if (ema9>ema20 and last>=ema9) else 0.0
    hh60 = hh_pos_60m(cs1m)

    # نقاط مطبّقة
    z_speed = min(1.0, spd/1.2)                        # 1.2 صفقة/ث للعلامة الكاملة
    z_ur    = min(1.0, (ur-0.5)/0.25) if ur>0.5 else 0
    z_vol   = min(1.0, volty/1.6)                      # ~0.3% حركة تراكمية ≈ 1.6
    z_rvol  = min(1.0, (rvol-0.9)/0.6) if rvol>0.9 else 0
    z_hh    = max(0.0, (hh60-0.6)/0.4)                 # نفضل أعلى 40% من النطاق

    w_speed = 0.32 if AGGR else 0.25
    w_ur    = 0.18
    w_vol   = 0.20
    w_rvol  = 0.15
    w_trend = 0.15 if AGGR else 0.22

    score = w_speed*z_speed + w_ur*z_ur + w_vol*z_vol + w_rvol*z_rvol + w_trend*(0.6*align + 0.4*above_vwap) + 0.10*z_hh

    why = f"ur={ur:.2f} {color_emoji(ur,0.62,0.54)} | spd={spd:.2f} {color_emoji(spd,0.9,0.5)} | " \
          f"rvol={rvol:.2f} {color_emoji(rvol,1.20,1.00)} | vol1m~{volty:.2f} {color_emoji(volty,1.4,0.8)} | " \
          f"EMA9>20={int(align)} | vwap={int(above_vwap)} | HH60={hh60:.2f}"

    return score, {"why": why, "ur":ur, "spd":spd, "rvol":rvol, "vol":volty, "align":align, "hh":hh60}

# ===== Gap / orderbook dominance (خفيف) =====
def engine_gap(market, cache):
    bk = cache.get(("book",market))
    if bk is None:
        bk = book(market,3); cache[("book",market)] = bk
    if not bk: return 0.0, {}
    spr = bk["spread_pct"]; B=bk["depth_bid_eur"]; A=bk["depth_ask_eur"]
    if A<=0 or B<=0: return 0.0, {}
    ratio = B/max(A,1e-9)
    score = 0.0
    if spr<=MAX_SPREAD and ratio>=1.6:
        score = min(1.0, 0.35 + (ratio-1.6)/3.0)
    why = f"gap r={ratio:.2f}, spr={spr:.2f}%"
    return score, {"why": why, "spr": spr, "B":B, "A":A, "bids":B, "asks":A}

# ===== حُرّاس خفاف + عقوبات =====
def guards_and_penalties(market, meta, want_eur):
    bk = book(market,3)
    if not bk: return False, "no_book", 0.0
    slip = estimate_slippage_pct(bk["asks"], want_eur)
    spr  = bk["spread_pct"]
    depA = bk["depth_ask_eur"]
    # رفض صارم فقط لو شي خارج الحدود جداً
    if spr > MAX_SPREAD*1.6: return False, "spread_hard", 0.0
    if depA < DEPTH_MIN*0.6: return False, "depth_hard", 0.0
    if slip > MAX_SLIP*1.8:  return False, "slip_hard", 0.0
    # عقوبات ناعمة تدخل ضمن السكور
    pen = 0.0
    if spr > MAX_SPREAD: pen += min(0.25, (spr-MAX_SPREAD)/MAX_SPREAD*0.18)
    if slip > MAX_SLIP:  pen += min(0.30, (slip-MAX_SLIP)/MAX_SLIP*0.22)
    return True, "ok", pen

# ===== تعلّم =====
def learn_update(coin, pnl_eur, reason):
    L = LEARN.get(coin, {"pnl_ema":0.0,"win_ema":0.5,"adj":0.0})
    a=0.3
    win = 1.0 if reason in ("tp_filled","manual_sell_filled") else (0.0 if reason in ("sl_triggered","taker_failed","buy_failed") else 0.5)
    L["pnl_ema"] = (1-a)*L["pnl_ema"] + a*(pnl_eur or 0.0)
    L["win_ema"] = (1-a)*L["win_ema"] + a*win
    L["adj"]     = max(-0.08, min(0.12, 0.12*(0.55 - L["win_ema"])))
    LEARN[coin]=L

def adjusted_star(coin):
    base = 0.50 if AGGR else SCORE_STAR
    return max(0.45, min(0.92, base + (LEARN.get(coin,{}).get("adj",0.0))))

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
            tg_send(f"🚀 أرسلت {coin} إلى صقر — {why_line}")
        else:
            report_error("send_buy", f"HTTP {r.status_code} | {r.text[:140]}")
    except Exception as e:
        report_error("send_buy", f"{type(e).__name__}: {e}")

# ===== اختيار وإطلاق =====
def pick_and_emit(cache, markets):
    best=None
    for m in markets:
        s_pulse, metaP = engine_pulse(m, cache)
        s_gap  , metaG = engine_gap(m, cache)

        # نبض أدنى (فلتر رخيص وسريع)
        if metaP["spd"] < (0.45 if AGGR else 0.35) and metaP["rvol"] < 1.05 and metaP["vol"] < 0.7:
            # عدّه dull
            coin = m.split("-")[0]
            ct = DULL_FAILS.get(coin, (0,0))
            cnt, ts = ct
            if time.time()-ts > 600: cnt=0
            cnt+=1; DULL_FAILS[coin]=(cnt, time.time())
            if cnt>=2 and COOLDOWN_UNTIL.get(coin,0) < time.time():
                COOLDOWN_UNTIL[coin] = time.time() + (1200 if AGGR else 2400)  # 20–40 دقيقة
            continue

        ok, why_guard, penalty = guards_and_penalties(m, {**metaP, **metaG}, BUY_EUR)
        if not ok: 
            continue

        # جمع درجات (نعاقب السبريد/الانزلاق)
        w_pulse = 0.80
        w_gap   = 0.20
        raw_score = w_pulse*s_pulse + w_gap*s_gap
        score = max(0.0, raw_score - penalty)

        coin=m.split("-")[0]
        star = adjusted_star(coin)

        if score >= star:
            why = f"{metaP['why']} | {metaG.get('why','')} | pen={penalty:.2f} | ⭐{score:.2f}/{star:.2f}"
            if not best or score > best[0]:
                best=(score, coin, why)

        # وضع هجومي: لو النبض قوي جداً أطلق فوراً
        if AGGR and s_pulse>=0.92:
            send_buy(coin, f"fast pulse ⭐{s_pulse:.2f}")
            return True

    if best:
        send_buy(best[1], best[2]); return True
    return False

# ===== ترتيب حسب السيولة =====
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
                time.sleep(0.12); continue
            try:
                if time.time()-hot_t >= 1.0:
                    if pick_and_emit(cache, HOT): return
                    hot_t=time.time()
            except Exception as e:
                report_error("hot loop", f"{type(e).__name__}: {e}")
            try:
                if time.time()-scout_t >= (2.3 if AGGR else 3.8):
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
    return f"Express Pro v5 ✅ run={RUN_ID} | aggr={AGGR} | learn={len(LEARN)}", 200

# ===== Main =====
if __name__=="__main__":
    port = int(os.getenv("PORT","8082"))
    tg_send("⚡️ Express Pro v5 — started.")
    app.run("0.0.0.0", port, threaded=True)