# -*- coding: utf-8 -*-
"""
Auto-Signal Scanner — STP Top-1 (Bitvavo EUR → Saqar)
Sleep-Then-Pulse: يرصد نبضة قوية بعد ساعة هدوء، يختار Top-1 ويرسله لصقر.

- فحص فوري عند الإقلاع ثم كل 15 دقيقة (افتراضيًا).
- فلتر نوم: ضيق المدى/BB منخفضة في آخر ساعة.
- نبضة: +% على 1m/5m + مضاعفة حجم 1m + تدفق شراء + سبريد/OB مقبول.
"""

import os, time, math, statistics as st
from threading import Thread
import ccxt
import pandas as pd
from dotenv import load_dotenv
import requests
from flask import Flask, request, jsonify

load_dotenv()
app = Flask(__name__)

# ====== ENV / Defaults ======
EXCHANGE   = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE      = os.getenv("QUOTE", "EUR").upper()

REQUEST_SLEEP_MS   = int(os.getenv("REQUEST_SLEEP_MS", "120"))

# ---- Sleep (ساعة) ----
SLEEP_RANGE_MAX_PCT = float(os.getenv("SLEEP_RANGE_MAX_PCT", "0.8"))   # أقصى مدى % خلال ساعة
SLEEP_BB_PCTL       = float(os.getenv("SLEEP_BB_PCTL", "25"))          # BB bandwidth ≤ هذا البرسينتايل (تاريخياً 4h)

# ---- Pulse ----
PULSE_1M_MIN_PCT = float(os.getenv("PULSE_1M_MIN_PCT", "0.6"))
PULSE_5M_MIN_PCT = float(os.getenv("PULSE_5M_MIN_PCT", "0.8"))
VOL1M_SPIKE_X    = float(os.getenv("VOL1M_SPIKE_X", "3.0"))
FLOW_MIN         = float(os.getenv("FLOW_MIN", "0.60"))
BID_IMB_MIN      = float(os.getenv("BID_IMB_MIN", "1.4"))
MAX_SPREAD_BP    = float(os.getenv("MAX_SPREAD_BP", "60"))
PUMP_CAP_1M_PCT  = float(os.getenv("PUMP_CAP_1M_PCT", "3.5"))

PULSE_SCORE_MIN  = float(os.getenv("PULSE_SCORE_MIN", "1.6"))

# ---- Universe / cadence ----
TOP_UNIVERSE       = int(os.getenv("TOP_UNIVERSE", "120"))   # أعلى أزواج EUR بالحجم
AUTO_SCAN_ENABLED  = int(os.getenv("AUTO_SCAN_ENABLED", "1"))
AUTO_PERIOD_SEC    = int(os.getenv("AUTO_PERIOD_SEC", "900"))   # 900s = 15m
SIGNAL_COOLDOWN_SEC= int(os.getenv("SIGNAL_COOLDOWN_SEC", "900")) # تبريد الإرسال لنفس العملة
TTL_SEC            = int(os.getenv("TTL_SEC", "60"))

# ---- Telegram ----
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()

def tg_send(text, chat_id=None):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id or CHAT_ID, "text": text,
                            "parse_mode":"Markdown", "disable_web_page_preview":True}, timeout=15)
    except Exception as e:
        print("tg err:", e)

# ---- Saqar ----
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "").strip()

_LAST_SIGNAL_TS = {}
def _can_signal(base): return (time.time()-_LAST_SIGNAL_TS.get(base,0))>=SIGNAL_COOLDOWN_SEC
def _mark(base): _LAST_SIGNAL_TS[base]=time.time()

def send_saqar(base, score=None, meta=None):
    if not SAQAR_WEBHOOK:
        tg_send("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    payload = {"cmd":"buy","coin":base}
    if score is not None: payload["confidence"] = float(score)
    if meta: payload["meta"] = meta
    try:
        r = requests.post(SAQAR_WEBHOOK.rstrip("/")+"/hook", json=payload, timeout=8)
        tg_send(f"📡 صقر ← buy {base} | status={r.status_code} | resp=`{(r.text or '')[:200]}`")
        return 200 <= r.status_code < 300
    except Exception as e:
        tg_send(f"❌ فشل إرسال لصقر: `{e}`"); return False

# ====== Helpers / Exchange ======
def make_exchange(name): return getattr(ccxt, name)({"enableRateLimit": True})
def now_ms(): return int(time.time()*1000)
def diplomatic_sleep(ms): time.sleep(ms/1000.0)
def safe(x, d=float("nan")):
    try: return float(x)
    except: return d
def pct(a,b):
    try:
        if b in (0,None): return float("nan")
        return (a-b)/b*100.0
    except: return float("nan")

def list_quote_markets(ex, quote="EUR", top_n=TOP_UNIVERSE):
    markets = ex.load_markets()
    try:
        tix = ex.fetch_tickers()
    except Exception:
        out = [m for m,info in markets.items() if info.get("active",True) and info.get("quote")==quote]
        return out[:top_n]
    rows=[]
    for sym, info in markets.items():
        if not info.get("active", True) or info.get("quote")!=quote: continue
        tk = tix.get(sym) or {}
        last = tk.get("last") or tk.get("close")
        base_vol = tk.get("baseVolume") or (tk.get("info",{}) or {}).get("volume")
        try: qvol = float(base_vol)*float(last) if base_vol and last else 0.0
        except: qvol = 0.0
        rows.append((sym,qvol))
    rows.sort(key=lambda x:x[1], reverse=True)
    return [s for s,_ in rows[:top_n]]

def get_ob(ex, sym, depth=25):
    try:
        ob = ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        bid = safe(ob["bids"][0][0]); ask = safe(ob["asks"][0][0])
        if bid<=0 or ask<=0: return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        spread = (ask - bid)/((ask + bid)/2)*10000.0
        bidvol = sum(safe(p[1],0.0) for p in ob["bids"][:5])
        askvol = sum(safe(p[1],0.0) for p in ob["asks"][:5])
        return {"spread_bp": spread, "bid_imb": bidvol/max(askvol,1e-9)}
    except Exception:
        return {"spread_bp": float("nan"), "bid_imb": float("nan")}

def get_flow(ex, sym, sec=120):
    try:
        trs = ex.fetch_trades(sym, since=now_ms()-sec*1000, limit=200)
        if not trs: return {"buy_take_ratio": float("nan")}
        def amt(t): return safe(t.get("amount", t.get("size",0.0)), 0.0)
        bv = sum(amt(t) for t in trs if (t.get("side") or "").lower()=="buy")
        tot= sum(amt(t) for t in trs)
        return {"buy_take_ratio": bv/max(tot,1e-9)}
    except Exception:
        return {"buy_take_ratio": float("nan")}

def fetch_ohlcv(ex, sym, tf, limit):
    try: return ex.fetch_ohlcv(sym, tf, limit=limit)
    except Exception: return []

# ====== Sleep metrics ======
def bb_bandwidth(closes, window=20, k=2):
    if len(closes)<window: return float("nan")
    seg=closes[-window:]; mu=sum(seg)/len(seg)
    sd=(sum((c-mu)**2 for c in seg)/len(seg))**0.5
    return (2*k*sd)/mu*100.0 if mu>0 else float("nan")

def sleep_features_1h(ex, sym):
    # 1m: آخر 60 دقيقة لإيجاد المدى والـBB الآن، و4 ساعات لتقدير البرسينتايل
    o1h = fetch_ohlcv(ex, sym, "1m", 240)  # ~4h
    if not o1h or len(o1h)<70: return None
    closes = [safe(x[4]) for x in o1h]
    last60 = closes[-60:]
    if min(last60)<=0: return None
    rng = (max(last60)-min(last60))/((max(last60)+min(last60))/2)*100.0
    bb_now = bb_bandwidth(last60, window=20, k=2)
    # تاريخي (تقريبي): roll BB على كامل 4h
    hist=[]
    for i in range(40, len(closes)+1):
        hist.append(bb_bandwidth(closes[:i][-60:], 20, 2))
    hist=[h for h in hist if math.isfinite(h)]
    if not hist or not math.isfinite(bb_now): bb_pctl = float("nan")
    else:
        rank=sum(1 for h in hist if h<=bb_now); bb_pctl = rank/len(hist)*100.0
    # أحجام 1m
    vols = [safe(x[5],0.0) for x in o1h][-60:]
    vmed = st.median(vols[:-1]) if len(vols)>1 else 0.0
    vnow = vols[-1] if vols else 0.0
    return {"range_pct": rng, "bb_pctl": bb_pctl, "vol1m_now": vnow, "vol1m_med": vmed, "last60_closes": last60}

# ====== Pulse metrics ======
def pulse_features(ex, sym, last60_closes, vol1m_med):
    # تغير 1m و5m + مضاعفة الحجم + حارس المضخات
    o5 = fetch_ohlcv(ex, sym, "5m", 13)
    if not o5 or len(o5)<3: return None
    pct1m = pct(last60_closes[-1], last60_closes[-2])
    pct5m = pct(o5[-1][4], o5[-2][4])
    # spike multiple
    v1m_now = safe(fetch_ohlcv(ex, sym, "1m", 2)[-1][5], 0.0)
    vol_mult = (v1m_now / max(vol1m_med, 1e-9)) if vol1m_med>0 else float("nan")
    pump_guard = max(abs(pct(o5[i][4], o5[i-1][4])) for i in range(1,len(o5)))
    return {"pct1m": pct1m, "pct5m": pct5m, "vol_mult": vol_mult, "pump_guard": pump_guard}

def get_ob_ok(ex, sym):
    ob = get_ob(ex, sym)
    return ob, (math.isfinite(ob["spread_bp"]) and ob["spread_bp"]<=MAX_SPREAD_BP
                and math.isfinite(ob["bid_imb"]) and ob["bid_imb"]>=BID_IMB_MIN)

def stp_score(p1m, p5m, volx, flow, imb, spr, sleep_range, bbpctl):
    # نبضة قوية + سيولة + سبريد جيد + نوم أوضح
    def norm(x, lo, hi):
        if not math.isfinite(x): return 0.0
        return max(0.0, min(1.0, (x-lo)/max(hi-lo,1e-9)))
    s = 0.0
    s += 1.1*norm(p1m, 0.6, 2.0)
    s += 1.0*norm(volx, 2.0, 8.0)
    s += 0.6*norm(flow, 0.5, 0.9)
    s += 0.4*norm(imb, 1.0, 2.0)
    s -= 0.4*norm(spr, 15.0, 70.0)
    # النوم: كلما كان المدى أضيق والـpctl أقل كان أفضل
    s += 0.3*norm(max(1.2 - sleep_range, 0), 0.0, 1.2)   # عكسي
    s += 0.3*norm(max(50.0 - bbpctl, 0), 0.0, 50.0)       # عكسي
    return s

# ====== Scanner ======
def scan_stp_top1():
    ex = make_exchange(EXCHANGE)
    syms = list_quote_markets(ex, QUOTE, top_n=TOP_UNIVERSE)

    cands=[]
    for sym in syms:
        try:
            # 1) نوم
            sf = sleep_features_1h(ex, sym)
            if not sf: continue
            asleep = (math.isfinite(sf["range_pct"]) and sf["range_pct"]<=SLEEP_RANGE_MAX_PCT) and \
                     (math.isfinite(sf["bb_pctl"])   and sf["bb_pctl"]  <=SLEEP_BB_PCTL)
            if not asleep: 
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue

            # 2) نبضة
            pf = pulse_features(ex, sym, sf["last60_closes"], sf["vol1m_med"])
            if not pf: 
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            if not (math.isfinite(pf["pct1m"]) and pf["pct1m"]>=PULSE_1M_MIN_PCT): 
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            if not (math.isfinite(pf["pct5m"]) and pf["pct5m"]>=PULSE_5M_MIN_PCT):
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            if not (math.isfinite(pf["vol_mult"]) and pf["vol_mult"]>=VOL1M_SPIKE_X):
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            if math.isfinite(pf["pump_guard"]) and pf["pump_guard"]>PUMP_CAP_1M_PCT:
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue

            # 3) OB/Flow
            ob, ob_ok = get_ob_ok(ex, sym)
            flow = get_flow(ex, sym, sec=120)["buy_take_ratio"]
            if not ob_ok:
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            if not (math.isfinite(flow) and flow>=FLOW_MIN):
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue

            score = stp_score(pf["pct1m"], pf["pct5m"], pf["vol_mult"], flow,
                              ob["bid_imb"], ob["spread_bp"], sf["range_pct"], sf["bb_pctl"])
            if score >= PULSE_SCORE_MIN:
                cands.append({
                    "symbol": sym, "score": score,
                    "pct1m": pf["pct1m"], "pct5m": pf["pct5m"], "volx": pf["vol_mult"],
                    "flow": flow, "imb": ob["bid_imb"], "spr": ob["spread_bp"],
                    "sleep_range": sf["range_pct"], "bb_pctl": sf["bb_pctl"]
                })

        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)

    if not cands:
        # Fallback خفيف
        fb=[]
        for sym in syms[:60]:
            try:
                sf = sleep_features_1h(ex, sym)
                if not sf: continue
                asleep = (sf["range_pct"]<=SLEEP_RANGE_MAX_PCT*1.1) and (sf["bb_pctl"]<=SLEEP_BB_PCTL+10)
                if not asleep: continue
                pf = pulse_features(ex, sym, sf["last60_closes"], sf["vol1m_med"])
                if not pf: continue
                if not (pf["pct1m"]>=max(0.5, PULSE_1M_MIN_PCT-0.1) and pf["vol_mult"]>=max(2.5, VOL1M_SPIKE_X-0.3)): 
                    continue
                ob = get_ob(ex, sym)
                score = stp_score(pf["pct1m"], pf["pct5m"], pf["vol_mult"], float("nan"),
                                  ob["bid_imb"], ob["spread_bp"], sf["range_pct"], sf["bb_pctl"])
                fb.append({"symbol": sym, "score": score, "pct1m": pf["pct1m"], "pct5m": pf["pct5m"],
                           "volx": pf["vol_mult"], "flow": float("nan"), "imb": ob["bid_imb"],
                           "spr": ob["spread_bp"], "sleep_range": sf["range_pct"], "bb_pctl": sf["bb_pctl"], "fallback": True})
            except Exception:
                pass
            diplomatic_sleep(REQUEST_SLEEP_MS)
        cands = fb

    if not cands:
        return pd.DataFrame()

    df = pd.DataFrame(cands)
    df.sort_values(by=["score","pct1m","volx"], ascending=[False,False,False], inplace=True)
    return df.head(1).reset_index(drop=True)

# ====== Run / Report ======
def run_and_report(chat=None):
    try:
        df = scan_stp_top1()
        if df is None or not len(df):
            tg_send("ℹ️ STP: لا نبض صالح الآن.", chat); return

        c = df.iloc[0].to_dict()
        base = c["symbol"].split("/")[0]
        msg = (f"⚡️ STP Top1: *{c['symbol']}*\n"
               f"sleep: range {c['sleep_range']:.2f}% | BB pctl {c['bb_pctl']:.0f}\n"
               f"pulse: +{c['pct1m']:.2f}%/1m, +{c['pct5m']:.2f}%/5m, vol×{c['volx']:.2f}\n"
               f"OB {c['imb']:.2f} | spr {c['spr']:.0f}bp | score {c['score']:.2f}"
               f"{' (fallback)' if c.get('fallback') else ''} ✅")
        tg_send(msg, chat)

        if _can_signal(base) and send_saqar(base, score=c["score"], meta={
            "sleep_range": round(c["sleep_range"],3),
            "bb_pctl": round(c["bb_pctl"],1),
            "pct1m": round(c["pct1m"],3),
            "pct5m": round(c["pct5m"],3),
            "volx": round(c["volx"],2),
            "spread_bp": round(c["spr"],1),
            "imb": round(c["imb"],2) if math.isfinite(c["imb"]) else None,
            "ttl": TTL_SEC
        }):
            _mark(base)
        else:
            tg_send("⏸ تخطّيت الإرسال (cooldown أو فشل الشبكة).", chat)

    except ccxt.BaseError as e:
        tg_send(f"🐞 ccxt: {getattr(e,'__dict__',{}) or str(e)}", chat)
    except Exception as e:
        tg_send(f"🐞 runandreport: {e}", chat)

# ====== Auto loop ======
def auto_scan_loop():
    if not AUTO_SCAN_ENABLED:
        tg_send("🤖 Auto-Scan معطّل."); return
    tg_send(f"🤖 STP Auto-Scan: تشغيل فوري ثم كل {AUTO_PERIOD_SEC}s | Top{TOP_UNIVERSE} {QUOTE}")
    # فحص فوري عند الإقلاع
    try: run_and_report()
    except Exception as e: tg_send(f"🐞 First scan: {e}")
    # ثم كل 15 دقيقة
    while True:
        try: run_and_report()
        except Exception as e: tg_send(f"🐞 AutoScan error: `{e}`")
        time.sleep(max(60, AUTO_PERIOD_SEC))

# ====== HTTP ======
@app.route("/", methods=["GET"])
def health(): return "ok", 200

@app.route("/webhook", methods=["POST"])
def tg_webhook():
    try:
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", CHAT_ID))
        if text.startswith("/scan"):
            tg_send("⏳ بدأ فحص STP بالخلفية…", chat_id)
            Thread(target=run_and_report, args=(chat_id,), daemon=True).start()
        else:
            tg_send("أوامر: /scan", chat_id)
        return jsonify(ok=True), 200
    except Exception as e:
        print("Webhook error:", e)
        return jsonify(ok=True), 200

# ====== Main ======
if __name__ == "__main__":
    Thread(target=auto_scan_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))