# -*- coding: utf-8 -*-
"""
Auto-Signal Scanner — STP Top-1 with Adaptive Hunt (Bitvavo EUR → Saqar)

Sleep-Then-Pulse:
- فحص فوري عند الإقلاع.
- كل 15 دقيقة: 5 دقائق HUNT (كل 60s) بمستويات عتبات متدرجة → ثم 10 دقائق راحة.
- إرسال Top-1 فقط إلى Saqar عبر /hook.
- بعد 3 جولات بلا أي إشارة → Fallback انتقائي.
- تيليغرام: تشخيص واضح + heartbeat.
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

# ====== ENV ======
EXCHANGE = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE    = os.getenv("QUOTE", "EUR").upper()

REQUEST_SLEEP_MS = int(os.getenv("REQUEST_SLEEP_MS", "120"))

# Sleep (1h)
SLEEP_RANGE_MAX_PCT = float(os.getenv("SLEEP_RANGE_MAX_PCT", "0.8"))
SLEEP_BB_PCTL       = float(os.getenv("SLEEP_BB_PCTL", "25"))

# Pulse base thresholds (Level 0)
PULSE_1M_MIN_PCT_L0 = float(os.getenv("PULSE_1M_MIN_PCT", "0.6"))
PULSE_5M_MIN_PCT    = float(os.getenv("PULSE_5M_MIN_PCT", "0.8"))
VOL1M_SPIKE_X_L0    = float(os.getenv("VOL1M_SPIKE_X", "3.0"))
FLOW_MIN_L0         = float(os.getenv("FLOW_MIN", "0.60"))
BID_IMB_MIN         = float(os.getenv("BID_IMB_MIN", "1.4"))
MAX_SPREAD_BP_L0    = float(os.getenv("MAX_SPREAD_BP", "60"))
PUMP_CAP_1M_PCT     = float(os.getenv("PUMP_CAP_1M_PCT", "3.5"))

PULSE_SCORE_MIN     = float(os.getenv("PULSE_SCORE_MIN", "1.6"))

# Universe & cadence
TOP_UNIVERSE      = int(os.getenv("TOP_UNIVERSE", "120"))
AUTO_SCAN_ENABLED = int(os.getenv("AUTO_SCAN_ENABLED", "1"))
CYCLE_PERIOD_SEC  = int(os.getenv("CYCLE_PERIOD_SEC", "900"))     # 15m
HUNT_WINDOW_MIN   = int(os.getenv("HUNT_WINDOW_MIN", "5"))        # 5m hunt
HUNT_STEP_SEC     = int(os.getenv("HUNT_STEP_SEC", "60"))         # every 60s
TTL_SEC           = int(os.getenv("TTL_SEC", "60"))
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "900"))

# Telegram / Saqar
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "").strip()

# ====== Globals ======
_LAST_SIGNAL_TS = {}
_empty_cycles = 0  # عدد الجولات 15m المتتالية بلا إشارات

# ====== Utils ======
def tg_send(text, chat_id=None):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id or CHAT_ID, "text": text,
                            "parse_mode":"Markdown", "disable_web_page_preview":True}, timeout=15)
    except Exception as e: print("tg err:", e)

def saqar_send(base, score=None, meta=None):
    if not SAQAR_WEBHOOK:
        tg_send("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    payload = {"cmd":"buy","coin":base}
    if score is not None: payload["confidence"]=float(score)
    if meta: payload["meta"]=meta
    try:
        r=requests.post(SAQAR_WEBHOOK.rstrip("/")+"/hook", json=payload, timeout=8)
        tg_send(f"📡 صقر ← buy {base} | status={r.status_code} | resp=`{(r.text or '')[:200]}`")
        return 200<=r.status_code<300
    except Exception as e:
        tg_send(f"❌ فشل إرسال لصقر: `{e}`"); return False

def _can_signal(base): return (time.time()-_LAST_SIGNAL_TS.get(base,0))>=SIGNAL_COOLDOWN_SEC
def _mark(base): _LAST_SIGNAL_TS[base]=time.time()

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
    for sym,info in markets.items():
        if not info.get("active",True) or info.get("quote")!=quote: continue
        tk=tix.get(sym) or {}
        last=tk.get("last") or tk.get("close")
        base_vol=tk.get("baseVolume") or (tk.get("info",{}) or {}).get("volume")
        try: qvol=float(base_vol)*float(last) if base_vol and last else 0.0
        except: qvol=0.0
        rows.append((sym,qvol))
    rows.sort(key=lambda x:x[1], reverse=True)
    return [s for s,_ in rows[:top_n]]

def get_ob(ex, sym, depth=25):
    try:
        ob = ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        bid=safe(ob["bids"][0][0]); ask=safe(ob["asks"][0][0])
        if bid<=0 or ask<=0: return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        spread=(ask-bid)/((ask+bid)/2)*10000.0
        bidvol=sum(safe(p[1],0.0) for p in ob["bids"][:5])
        askvol=sum(safe(p[1],0.0) for p in ob["asks"][:5])
        return {"spread_bp": spread, "bid_imb": bidvol/max(askvol,1e-9)}
    except Exception:
        return {"spread_bp": float("nan"), "bid_imb": float("nan")}

def get_flow(ex, sym, sec=120):
    try:
        trs=ex.fetch_trades(sym, since=now_ms()-sec*1000, limit=200)
        if not trs: return {"buy_take_ratio": float("nan")}
        def amt(t): return safe(t.get("amount", t.get("size",0.0)), 0.0)
        bv=sum(amt(t) for t in trs if (t.get("side") or "").lower()=="buy")
        tot=sum(amt(t) for t in trs)
        return {"buy_take_ratio": bv/max(tot,1e-9)}
    except Exception:
        return {"buy_take_ratio": float("nan")}

def fetch_ohlcv(ex, sym, tf, limit):
    try: return ex.fetch_ohlcv(sym, tf, limit=limit)
    except Exception: return []

# ====== Sleep features ======
def bb_bandwidth(closes, window=20, k=2):
    if len(closes)<window: return float("nan")
    seg=closes[-window:]; mu=sum(seg)/len(seg)
    sd=(sum((c-mu)**2 for c in seg)/len(seg))**0.5
    return (2*k*sd)/mu*100.0 if mu>0 else float("nan")

def sleep_features_1h(ex, sym):
    o1m = fetch_ohlcv(ex, sym, "1m", 240)  # ~4h
    if not o1m or len(o1m)<70: return None
    closes=[safe(x[4]) for x in o1m]
    last60=closes[-60:]
    rng=(max(last60)-min(last60))/((max(last60)+min(last60))/2)*100.0
    bb_now=bb_bandwidth(last60, 20, 2)
    hist=[]
    for i in range(40, len(closes)+1):
        hist.append(bb_bandwidth(closes[:i][-60:], 20, 2))
    hist=[h for h in hist if math.isfinite(h)]
    if not hist or not math.isfinite(bb_now): bb_pctl=float("nan")
    else:
        rank=sum(1 for h in hist if h<=bb_now); bb_pctl=rank/len(hist)*100.0
    vols=[safe(x[5],0.0) for x in o1m][-60:]
    vmed=st.median(vols[:-1]) if len(vols)>1 else 0.0
    return {"range_pct": rng, "bb_pctl": bb_pctl, "vmed1m": vmed, "closes60": last60}

# ====== Pulse features ======
def pulse_features(ex, sym, closes60, vmed1m):
    o5=fetch_ohlcv(ex, sym, "5m", 13)
    if not o5 or len(o5)<3: return None
    pct1m=pct(closes60[-1], closes60[-2])
    pct5m=pct(o5[-1][4], o5[-2][4])
    v1m_now = safe(fetch_ohlcv(ex, sym, "1m", 2)[-1][5], 0.0)
    volx = (v1m_now / max(vmed1m,1e-9)) if vmed1m>0 else float("nan")
    pump_guard=max(abs(pct(o5[i][4], o5[i-1][4])) for i in range(1,len(o5)))
    return {"pct1m": pct1m, "pct5m": pct5m, "volx": volx, "pump_guard": pump_guard}

def stp_score(p1m, p5m, volx, flow, imb, spr, sleep_range, bbpctl):
    def norm(x, lo, hi):
        if not math.isfinite(x): return 0.0
        return max(0.0, min(1.0, (x-lo)/max(hi-lo,1e-9)))
    s=0.0
    s += 1.1*norm(p1m, 0.6, 2.0)
    s += 1.0*norm(volx, 2.0, 8.0)
    s += 0.6*norm(flow, 0.5, 0.9)
    s += 0.4*norm(imb, 1.0, 2.0)
    s -= 0.4*norm(spr, 15.0, 70.0)
    s += 0.3*norm(max(1.2 - sleep_range, 0), 0.0, 1.2)
    s += 0.3*norm(max(50.0 - bbpctl, 0), 0.0, 50.0)
    return s

# ====== Adaptive thresholds by level ======
def thresholds_for_level(level:int):
    # level: 0..3
    if level==0:
        return dict(p1m=PULSE_1M_MIN_PCT_L0, volx=VOL1M_SPIKE_X_L0,
                    flow=FLOW_MIN_L0, spread=MAX_SPREAD_BP_L0, allow_na_flow=False)
    if level==1:
        return dict(p1m=max(0.5, PULSE_1M_MIN_PCT_L0-0.1), volx=VOL1M_SPIKE_X_L0+0.5,
                    flow=FLOW_MIN_L0, spread=MAX_SPREAD_BP_L0, allow_na_flow=False)
    if level==2:
        return dict(p1m=0.45, volx=VOL1M_SPIKE_X_L0+1.0,
                    flow=max(0.57, FLOW_MIN_L0-0.03), spread=MAX_SPREAD_BP_L0+10, allow_na_flow=False)
    # level 3: يسمح flow NA بشرط volx قوي
    return dict(p1m=0.45, volx=5.0, flow=None, spread=MAX_SPREAD_BP_L0, allow_na_flow=True)

# ====== Core scan once (by adaptive level) ======
def scan_once(level:int):
    ex = make_exchange(EXCHANGE)
    syms = list_quote_markets(ex, QUOTE, top_n=TOP_UNIVERSE)

    th = thresholds_for_level(level)
    sleep_ok=0; pulse1_ok=0; volx_ok=0; flow_ok=0; spread_ok=0
    cands=[]

    for sym in syms:
        try:
            sf = sleep_features_1h(ex, sym)
            if not sf: continue
            asleep = (sf["range_pct"]<=SLEEP_RANGE_MAX_PCT) and (sf["bb_pctl"]<=SLEEP_BB_PCTL)
            if not asleep: 
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            sleep_ok+=1

            pf = pulse_features(ex, sym, sf["closes60"], sf["vmed1m"])
            if not pf or not (math.isfinite(pf["pct1m"]) and pf["pct1m"]>=th["p1m"]): 
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            pulse1_ok+=1
            if not (math.isfinite(pf["pct5m"]) and pf["pct5m"]>=PULSE_5M_MIN_PCT):
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            if not (math.isfinite(pf["volx"]) and pf["volx"]>=th["volx"]):
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            volx_ok+=1
            if math.isfinite(pf["pump_guard"]) and pf["pump_guard"]>PUMP_CAP_1M_PCT:
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue

            ob = get_ob(ex, sym)
            if not (math.isfinite(ob["spread_bp"]) and ob["spread_bp"]<=th["spread"] and
                    math.isfinite(ob["bid_imb"]) and ob["bid_imb"]>=BID_IMB_MIN):
                diplomatic_sleep(REQUEST_SLEEP_MS); 
                continue
            spread_ok+=1

            fr = get_flow(ex, sym, sec=120)["buy_take_ratio"]
            if th["allow_na_flow"]:
                flow_ok+=1  # بنعتبره ماشي
            else:
                if not (math.isfinite(fr) and fr>=th["flow"]):
                    diplomatic_sleep(REQUEST_SLEEP_MS); 
                    continue
                flow_ok+=1

            score = stp_score(pf["pct1m"], pf["pct5m"], pf["volx"], fr if math.isfinite(fr) else float("nan"),
                              ob["bid_imb"], ob["spread_bp"], sf["range_pct"], sf["bb_pctl"])
            if score >= PULSE_SCORE_MIN or level>=2:
                cands.append({
                    "symbol": sym, "score": score,
                    "pct1m": pf["pct1m"], "pct5m": pf["pct5m"], "volx": pf["volx"],
                    "flow": fr if math.isfinite(fr) else None,
                    "imb": ob["bid_imb"], "spr": ob["spread_bp"],
                    "sleep_range": sf["range_pct"], "bb_pctl": sf["bb_pctl"], "level": level
                })
        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)

    # تشخيص إذا لا شيء
    if not cands:
        tg_send(f"🔎 STP lvl{level}: لا مرشح | sleep_ok={sleep_ok}/{TOP_UNIVERSE} | "
                f"pulse1_ok={pulse1_ok} | volx_ok={volx_ok} | flow_ok={flow_ok} | spread_ok={spread_ok}")
        return None

    df=pd.DataFrame(cands).sort_values(by=["score","pct1m","volx"], ascending=[False,False,False]).head(1)
    c=df.iloc[0].to_dict()
    return c

# ====== Fallback after many empty cycles ======
def scan_fallback():
    ex = make_exchange(EXCHANGE)
    syms = list_quote_markets(ex, QUOTE, top_n=min(150, TOP_UNIVERSE+30))
    best=None
    for sym in syms:
        try:
            sf = sleep_features_1h(ex, sym)
            if not sf: continue
            pf = pulse_features(ex, sym, sf["closes60"], sf["vmed1m"])
            if not pf: continue
            # شروط انتقائية آمنة
            if not (math.isfinite(pf["pct1m"]) and pf["pct1m"]>=0.5): continue
            if not (math.isfinite(pf["volx"]) and pf["volx"]>=3.0): continue
            ob=get_ob(ex, sym)
            if not (math.isfinite(ob["spread_bp"]) and ob["spread_bp"]<=70): continue
            fr=get_flow(ex, sym, sec=120)["buy_take_ratio"]
            score=stp_score(pf["pct1m"], pf["pct5m"], pf["volx"], fr if math.isfinite(fr) else float("nan"),
                            ob["bid_imb"], ob["spread_bp"], sf["range_pct"], sf["bb_pctl"])
            row={"symbol":sym,"score":score,"pct1m":pf["pct1m"],"pct5m":pf["pct5m"],"volx":pf["volx"],
                 "flow": fr if math.isfinite(fr) else None,"imb":ob["bid_imb"],"spr":ob["spread_bp"],
                 "sleep_range":sf["range_pct"],"bb_pctl":sf["bb_pctl"],"fallback":True}
            if (best is None) or (row["score"]>best["score"]): best=row
        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)
    return best

# ====== Send candidate to Saqar ======
def dispatch_candidate(c):
    base = c["symbol"].split("/")[0]
    msg = (f"⚡️ STP Top1 (lvl{c.get('level','-')}): *{c['symbol']}*\n"
           f"sleep {c['sleep_range']:.2f}% | BB {c['bb_pctl']:.0f}\n"
           f"pulse +{c['pct1m']:.2f}%/1m +{c['pct5m']:.2f}%/5m vol×{c['volx']:.2f}\n"
           f"OB {c['imb']:.2f} spr {c['spr']:.0f}bp | score {c['score']:.2f}"
           f"{' (fallback)' if c.get('fallback') else ''} ✅")
    tg_send(msg)

    if _can_signal(base) and saqar_send(base, score=c["score"], meta={
        "sleep_range": round(c["sleep_range"],3),
        "bb_pctl": round(c["bb_pctl"],1),
        "pct1m": round(c["pct1m"],3),
        "pct5m": round(c["pct5m"],3),
        "volx": round(c["volx"],2),
        "spread_bp": round(c["spr"],1),
        "imb": round(c["imb"],2) if math.isfinite(c["imb"]) else None,
        "ttl": TTL_SEC,
        "level": c.get("level", 9),
        "fallback": bool(c.get("fallback", False))
    }):
        _mark(base)
        return True
    else:
        tg_send("⏸ تخطّيت الإرسال (cooldown أو فشل الشبكة).")
        return False

# ====== Auto cycle (15m) with Hunt ======
def one_cycle_with_hunt():
    global _empty_cycles
    tg_send(f"⏱️ بدء دورة STP: نافذة صيد {HUNT_WINDOW_MIN}m كل {HUNT_STEP_SEC}s…")

    start=time.time()
    sent=False
    # 5 دقائق hunt بمستويات متدرجة كل دقيقة
    for minute in range(HUNT_WINDOW_MIN):
        level = min(minute, 3)  # 0..3
        c = scan_once(level)
        if c:
            if dispatch_candidate(c):
                sent=True
                break
        # قلب النبض
        tg_send(f"🫀 HUNT t={minute+1}/{HUNT_WINDOW_MIN} (lvl{level})")
        # انتظر حتى نهاية الدقيقة الحالية (مع مراعاة وقت المسح)
        elapsed = time.time()-start
        target = (minute+1)*HUNT_STEP_SEC
        time.sleep(max(1, target - elapsed))

    if sent:
        _empty_cycles = 0
        tg_send("✅ انتهت الدورة: تم إرسال مرشح.")
    else:
        _empty_cycles += 1
        tg_send("ℹ️ انتهت الدورة: لا مرشح. ندخل فترة راحة.")

    # Fallback بعد 3 دورات بلا إشارات
    if not sent and _empty_cycles >= 3:
        tg_send("🟡 3 دورات بلا إشارة — تشغيل Fallback انتقائي…")
        c = scan_fallback()
        if c:
            dispatch_candidate(c)
            _empty_cycles = 0
        else:
            tg_send("⚠️ حتى الـFallback لم يجد مرشحًا مناسبًا.")

def auto_loop():
    if not AUTO_SCAN_ENABLED:
        tg_send("🤖 Auto-Scan معطّل."); return
    tg_send("🤖 STP Auto-Scan: فحص فوري ثم دورات كل 15 دقيقة.")
    # فحص فوري
    try:
        one_cycle_with_hunt()
    except Exception as e:
        tg_send(f"🐞 first cycle: {e}")

    # كل 15 دقيقة: Hunt (5m) ثم راحة لباقي الوقت
    while True:
        t0=time.time()
        try:
            one_cycle_with_hunt()
        except Exception as e:
            tg_send(f"🐞 cycle error: {e}")
        # راحة حتى تكتمل الـ15 دقيقة
        elapsed=time.time()-t0
        sleep_left=max(60, CYCLE_PERIOD_SEC - elapsed)
        tg_send(f"😴 راحة {int(sleep_left)}s… (heartbeat)")
        time.sleep(sleep_left)

# ====== HTTP ======
@app.route("/", methods=["GET"])
def health(): return "ok", 200

@app.route("/webhook", methods=["POST"])
def tg_webhook():
    try:
        upd=request.get_json(silent=True) or {}
        msg=upd.get("message") or upd.get("edited_message") or {}
        text=(msg.get("text") or "").strip()
        chat_id=str(msg.get("chat", {}).get("id", CHAT_ID))
        if text.startswith("/scan"):
            tg_send("⏳ بدء دورة STP بالخلفية…", chat_id)
            Thread(target=one_cycle_with_hunt, daemon=True).start()
        else:
            tg_send("أوامر: /scan", chat_id)
        return jsonify(ok=True), 200
    except Exception as e:
        print("Webhook error:", e); return jsonify(ok=True), 200

# ====== Main ======
if __name__ == "__main__":
    Thread(target=auto_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))