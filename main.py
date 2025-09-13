# -*- coding: utf-8 -*-
"""
Top3 → One-Signal-Per-Quarter (Bitvavo / EUR → Saqar)
- كل 15 دقيقة: نافذة صيد قصيرة (HUNT) تفحص عدة مرات وتختار أفضل مرشّح واحد فقط لإرساله لصقر.
- يعرض Top3 على تيليغرام للمراقبة اليدوية.
"""

import os, time, math, statistics as st
from threading import Thread
import ccxt, requests, pandas as pd
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ===== ENV =====
EXCHANGE  = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE     = os.getenv("QUOTE", "EUR").upper()
TOP_UNIVERSE = int(os.getenv("TOP_UNIVERSE", "120"))

# Cadence (ربع ساعة ذكي)
CYCLE_PERIOD_SEC = int(os.getenv("CYCLE_PERIOD_SEC", "900"))  # 15m
HUNT_WINDOW_MIN  = int(os.getenv("HUNT_WINDOW_MIN", "3"))     # نافذة صيد 3 دقائق
HUNT_STEP_SEC    = int(os.getenv("HUNT_STEP_SEC", "30"))      # نفحص كل 30 ثانية داخل النافذة

REQUEST_SLEEP_MS = int(os.getenv("REQUEST_SLEEP_MS", "120"))

# فلاتر السكالبينغ (لطّفها/شدّها حسب السوق)
OSC_THR_PCT   = float(os.getenv("OSC_THR_PCT", "0.5"))   # ±0.5% دورة
SPREAD_MAX_BP = float(os.getenv("SPREAD_MAX_BP", "70"))  # 0.70%
VOLX_MIN      = float(os.getenv("VOLX_MIN", "2.2"))      # spike 1m ≥ 2.2×
PCT5M_MIN     = float(os.getenv("PCT5M_MIN", "0.5"))     # ≥ 0.5% (مطلقًا)
BID_IMB_MIN   = float(os.getenv("BID_IMB_MIN", "1.3"))
FLOW_MIN      = float(os.getenv("FLOW_MIN", "0.54"))

# Saqar + Telegram
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK","").strip()
BOT_TOKEN     = os.getenv("BOT_TOKEN","").strip()
CHAT_ID       = os.getenv("CHAT_ID","").strip()

# تبريد عام + منع إعادة نفس العملة في دورتين متتاليتين
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "1200"))  # 20m > 15m
_last_signal_ts = {}
_last_cycle_coin = None

# ===== Utilities =====
def tg(text):
    if not BOT_TOKEN:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode":"Markdown",
                            "disable_web_page_preview": True}, timeout=15)
    except Exception as e: print("tg err:", e)

def saqar_send(base, score=None, meta=None):
    if not SAQAR_WEBHOOK:
        tg("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    payload = {"cmd":"buy","coin": base}
    if score is not None: payload["confidence"] = float(score)
    if meta: payload["meta"] = meta
    try:
        r = requests.post(SAQAR_WEBHOOK.rstrip("/")+"/hook", json=payload, timeout=8)
        tg(f"📡 صقر ← buy {base} | status={r.status_code} | resp=`{(r.text or '')[:160]}`")
        return 200 <= r.status_code < 300
    except Exception as e:
        tg(f"❌ فشل إرسال لصقر: `{e}`"); return False

def can_signal(base):
    return (time.time() - _last_signal_ts.get(base, 0)) >= SIGNAL_COOLDOWN_SEC and base != _last_cycle_coin

def mark_signal(base):
    global _last_cycle_coin
    _last_signal_ts[base] = time.time()
    _last_cycle_coin = base

def make_ex(name): return getattr(ccxt, name)({"enableRateLimit": True})
def diplomatic_sleep(ms): time.sleep(ms/1000.0)
def safe(x, d=float("nan")):
    try: return float(x)
    except: return d
def pct(a,b):
    try:
        if b in (0,None): return float("nan")
        return (a-b)/b*100.0
    except: return float("nan")

# ===== Data helpers =====
def list_quote_markets(ex, quote="EUR", top_n=TOP_UNIVERSE):
    markets = ex.load_markets()
    try:
        tix = ex.fetch_tickers()
    except Exception:
        return [m for m,info in markets.items() if info.get("active",True) and info.get("quote")==quote][:top_n]
    rows=[]
    for sym, info in markets.items():
        if not info.get("active", True) or info.get("quote")!=quote: continue
        tk = tix.get(sym) or {}
        last = tk.get("last") or tk.get("close")
        base_vol = tk.get("baseVolume") or (tk.get("info",{}) or {}).get("volume")
        try: qvol = float(base_vol)*float(last) if base_vol and last else 0.0
        except: qvol = 0.0
        rows.append((sym, qvol))
    rows.sort(key=lambda x:x[1], reverse=True)
    return [s for s,_ in rows[:top_n]]

def fetch_ohlcv(ex, sym, tf, limit):
    try: return ex.fetch_ohlcv(sym, tf, limit=limit)
    except Exception: return []

def get_ob(ex, sym, depth=25):
    try:
        ob = ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        bid = safe(ob["bids"][0][0]); ask = safe(ob["asks"][0][0])
        spread = (ask - bid)/((ask + bid)/2) * 10000.0
        bidvol = sum(safe(p[1],0.0) for p in ob["bids"][:5])
        askvol = sum(safe(p[1],0.0) for p in ob["asks"][:5])
        return {"spread_bp": spread, "bid_imb": bidvol/max(askvol,1e-9)}
    except Exception:
        return {"spread_bp": float("nan"), "bid_imb": float("nan")}

def get_flow(ex, sym, sec=120):
    try:
        trs = ex.fetch_trades(sym, since=int(time.time()*1000) - sec*1000, limit=200)
        if not trs: return float("nan")
        def amt(t): return safe(t.get("amount", t.get("size", 0.0)), 0.0)
        bv  = sum(amt(t) for t in trs if (t.get("side") or "").lower()=="buy")
        tot = sum(amt(t) for t in trs)
        return bv/max(tot,1e-9)
    except Exception:
        return float("nan")

# ===== Oscillation metric (آخر ساعة) =====
def detect_cycles(closes, thr_pct=OSC_THR_PCT):
    if len(closes) < 40: 
        return 0, float("nan"), float("nan")
    thr = thr_pct/100.0
    piv = closes[0]; piv_i=0; dir=0
    swings=[]
    for i in range(1, len(closes)):
        move = (closes[i] - piv) / max(piv, 1e-12)
        if dir==0:
            if move >= thr: dir=1
            elif move <= -thr: dir=-1
        elif dir==1 and move <= -thr:
            amp_up   = (max(closes[piv_i:i+1]) - piv)/max(piv,1e-12)
            amp_down = abs(move)
            swings.append( ( (amp_up+amp_down)*50.0, i-piv_i ) )
            piv = closes[i]; piv_i=i; dir=-1
        elif dir==-1 and move >= thr:
            amp_down = (piv - min(closes[piv_i:i+1]))/max(piv,1e-12)
            amp_up   = abs(move)
            swings.append( ( (amp_down+amp_up)*50.0, i-piv_i ) )
            piv = closes[i]; piv_i=i; dir=1
    if not swings: return 0, float("nan"), float("nan")
    amps = [a for a,_ in swings]
    durs = [d for _,d in swings if d>0]
    med_amp = st.median(amps) if amps else float("nan")
    med_dur = st.median(durs) if durs else float("nan")
    return len(swings), med_amp, med_dur

# ===== Score =====
def osc_score(cycles, med_amp, med_dur):
    def norm(x, lo, hi):
        if not math.isfinite(x): return 0.0
        return max(0.0, min(1.0, (x-lo)/max(hi-lo,1e-9)))
    s = 0.0
    s += 0.9 * norm(cycles, 0, 18)
    s += 0.8 * norm(med_amp, 0.3, 1.5)
    s -= 0.5 * norm(med_dur, 10.0, 1.0)
    return s

def total_score(osc_s, volx, pct5m_abs, bid_imb, spread_bp, flow):
    def norm(x, lo, hi):
        if not math.isfinite(x): return 0.0
        return max(0.0, min(1.0, (x-lo)/max(hi-lo,1e-9)))
    s = 0.0
    s += osc_s
    s += 0.9 * norm(volx, 2.0, 8.0)
    s += 0.6 * norm(pct5m_abs, 0.5, 3.0)
    s += 0.4 * norm(bid_imb, 1.0, 2.0)
    s -= 0.4 * norm(spread_bp, 15.0, 70.0)
    s += 0.3 * norm(flow, 0.5, 0.8)
    return s

# ===== Scan once → returns Top3 (df) =====
def scan_top3_once(ex):
    syms = list_quote_markets(ex, QUOTE, top_n=TOP_UNIVERSE)
    rows=[]
    for sym in syms:
        try:
            o1 = fetch_ohlcv(ex, sym, "1m", 70)
            if not o1 or len(o1)<50: 
                diplomatic_sleep(REQUEST_SLEEP_MS); continue
            closes = [safe(x[4]) for x in o1][-61:]
            vol_med = st.median([safe(x[5],0.0) for x in o1][-60:-1]) or 0.0
            vol_now = safe(o1[-1][5], 0.0)
            volx = (vol_now/max(vol_med,1e-9)) if vol_med>0 else float("nan")
            cycles, med_amp, med_dur = detect_cycles(closes, OSC_THR_PCT)

            o5 = fetch_ohlcv(ex, sym, "5m", 3)
            if not o5 or len(o5)<2:
                diplomatic_sleep(REQUEST_SLEEP_MS); continue
            pct5 = pct(o5[-1][4], o5[-2][4])

            ob = get_ob(ex, sym)
            flow = get_flow(ex, sym, sec=120)

            # فلاتر الحد الأدنى
            if not (math.isfinite(ob["spread_bp"]) and ob["spread_bp"] <= SPREAD_MAX_BP): 
                continue
            if not (math.isfinite(volx) and volx >= VOLX_MIN): 
                continue
            if not (math.isfinite(ob["bid_imb"]) and ob["bid_imb"] >= BID_IMB_MIN):
                continue
            if not (math.isfinite(pct5) and abs(pct5) >= PCT5M_MIN):
                continue
            if math.isfinite(flow) and flow < FLOW_MIN:
                continue

            osc_s = osc_score(cycles, med_amp, med_dur)
            score = total_score(osc_s, volx, abs(pct5), ob["bid_imb"], ob["spread_bp"], flow)

            rows.append({
                "symbol": sym, "score": score,
                "cycles": cycles, "amp": med_amp, "dur": med_dur,
                "volx": volx, "pct5m": pct5, "imb": ob["bid_imb"], "spr": ob["spread_bp"],
                "flow": flow
            })
        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)

    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values(by=["score","volx","cycles"], ascending=[False,False,False]).head(3).reset_index(drop=True)
    return df

# ===== One quarter cycle with HUNT window =====
def one_quarter_cycle():
    global _last_cycle_coin
    _last_cycle_coin = None   # امنع تكرار نفس العملة في نفس الدورة التالية
    tg(f"⏱️ دورة ربع ساعة: نافذة صيد {HUNT_WINDOW_MIN}m كل {HUNT_STEP_SEC}s…")
    ex = make_ex(EXCHANGE)

    best_row = None
    last_df_for_tg = None
    t_start = time.time()

    steps = max(1, int((HUNT_WINDOW_MIN*60) / HUNT_STEP_SEC))
    for k in range(steps):
        df = scan_top3_once(ex)
        if df is not None and len(df):
            last_df_for_tg = df
            r0 = df.iloc[0].to_dict()
            if (best_row is None) or (r0["score"] > best_row["score"]):
                best_row = r0
        tg(f"🫀 HUNT {k+1}/{steps}")
        # انتظر حتى نهاية الخطوة
        elapsed = time.time() - t_start
        target  = (k+1)*HUNT_STEP_SEC
        time.sleep(max(1, target - elapsed))

    # عرض Top3 آخر مرة للمراقبة
    if last_df_for_tg is None or not len(last_df_for_tg):
        tg("ℹ️ Top3: لا مرشّحات مناسبة خلال النافذة.")
    else:
        lines=[]
        for i, r in last_df_for_tg.iterrows():
            lines.append(
                f"{'🥇' if i==0 else ('🥈' if i==1 else '🥉')} {r['symbol']}: "
                f"sc={r['score']:.2f} | vol×{r['volx']:.1f} | 5m={r['pct5m']:+.2f}% | "
                f"osc(cyc={int(r['cycles'])},amp≈{(r['amp'] or 0):.2f}%,dur≈{(r['dur'] or 0):.1f}m) | "
                f"OB {r['imb']:.2f} | spr {r['spr']:.0f}bp"
            )
        tg("🎯 *Top3 (آخر لقطة من النافذة)*\n" + "\n".join(lines))

    # إرسال واحد فقط (أفضل ما رصدناه في كل النافذة)
    if best_row:
        base = best_row["symbol"].split("/")[0]
        if can_signal(base):
            ok = saqar_send(base, score=best_row["score"], meta={
                "volx": round(float(best_row["volx"]),2),
                "pct5m": round(float(best_row["pct5m"]),3),
                "cycles": int(best_row["cycles"]),
                "amp_pct": round(float(best_row["amp"] or 0),3),
                "dur_min": round(float(best_row["dur"] or 0),2),
                "spread_bp": round(float(best_row["spr"]),1),
                "imb": round(float(best_row["imb"]),2),
                "ttl": 60
            })
            if ok: mark_signal(base)
        else:
            tg(f"⏸ تخطّيت الإرسال (cooldown أو نفس عملة الدورة السابقة): {base}")
    else:
        tg("ℹ️ انتهت النافذة بلا مرشح مرسَل.")

def auto_loop():
    tg("🤖 Top3 Quarter Mode: تشغيل فوري ثم كل 15 دقيقة.")
    # أول دورة فورية
    try: one_quarter_cycle()
    except Exception as e: tg(f"🐞 first cycle: {e}")

    while True:
        t0=time.time()
        try: one_quarter_cycle()
        except Exception as e: tg(f"🐞 cycle error: {e}")
        # راحة لباقي الربع ساعة
        elapsed=time.time()-t0
        sleep_left=max(60, CYCLE_PERIOD_SEC - elapsed)
        tg(f"😴 راحة {int(sleep_left)}s… (heartbeat)")
        time.sleep(sleep_left)

# ===== HTTP =====
@app.route("/", methods=["GET"])
def health(): return "ok", 200

@app.route("/webhook", methods=["POST"])
def tg_webhook():
    try:
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        if text.startswith("/scan"):
            tg("⏳ بدأ دورة ربع ساعة بالخلفية…")
            Thread(target=one_quarter_cycle, daemon=True).start()
        else:
            tg("أوامر: /scan")
        return jsonify(ok=True), 200
    except Exception as e:
        print("Webhook error:", e); return jsonify(ok=True), 200

# ===== Main =====
if __name__ == "__main__":
    Thread(target=auto_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))