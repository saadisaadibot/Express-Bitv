# -*- coding: utf-8 -*-
"""
Auto-Signal Scanner — Oscillation Top-1 (Bitvavo EUR → Saqar)
- يركّز على آخر ساعة فقط.
- يقيّم الذبذبات السريعة (±CAPTURE_MIN_PCT) وعدد الدورات وسعتها ومدّتها.
- يفلتر السبريد/السيولة/التدفّق ويختار Top-1 دائمًا ويرسل لصقر.
- يحافظ على /scan و Auto-Loop و Telegram بنفس النمط السابق.

الاعتمادات: ccxt, pandas, Flask, python-dotenv, requests
"""

import os, time, math, statistics as st
from threading import Thread
import ccxt
import pandas as pd
from dotenv import load_dotenv
import requests
from flask import Flask, request, jsonify

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

EXCHANGE   = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE      = os.getenv("QUOTE", "EUR").upper()

# معدل النوم بين النداءات لتخفيف الضغط (ms)
REQUEST_SLEEP_MS      = int(os.getenv("REQUEST_SLEEP_MS", "120"))

# --------- وضع Oscillation (ساعة واحدة) ----------
MODE                 = os.getenv("MODE", "oscillation").lower()     # "oscillation" | "pulse" | "mixed" (نستخدم oscillation هنا)
CAPTURE_MIN_PCT      = float(os.getenv("CAPTURE_MIN_PCT", "0.6"))   # عتبة ±% للدورة المقبولة
MAX_SPREAD_BP        = float(os.getenv("MAX_SPREAD_BP", "50"))      # 50 bp = 0.50%
VOL1M_MIN_EUR        = float(os.getenv("VOL1M_MIN_EUR", "1500"))    # حد أدنى سيولة 1m
VOL5M_MIN_EUR        = float(os.getenv("VOL5M_MIN_EUR", "7500"))    # حد أدنى سيولة 5m
PUMP_CAP_1M_PCT      = float(os.getenv("PUMP_CAP_1M_PCT", "3.5"))   # حماية ضخ 1m
BID_IMB_MIN          = float(os.getenv("BID_IMB_MIN", "1.3"))
FLOW_MIN             = float(os.getenv("FLOW_MIN", "0.55"))         # buy_take_ratio
OSC_THRESHOLD        = float(os.getenv("OSC_THRESHOLD", "1.8"))     # حد قبول السكور النهائي

# مراحل الفرز لتقليل النداءات
TOP_UNIVERSE         = int(os.getenv("TOP_UNIVERSE", "100"))        # Top EUR pairs
OB_PRE_TOP_N         = int(os.getenv("OB_PRE_TOP_N", "120"))        # نفحص OB/سبريد لنطاق أوسع قليلاً
VOL_PRE_TOP_N        = int(os.getenv("VOL_PRE_TOP_N", "60"))        # نفحص 5m لحزمة أصغر
FLOW_TOP_N           = int(os.getenv("FLOW_TOP_N", "25"))           # نفحص trades فقط لأفضل 25

# ----- Telegram -----
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()

def tg_send_text(text, chat_id=None):
    if not BOT_TOKEN: 
        print("TG:", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id or CHAT_ID, "text": text, "parse_mode":"Markdown", "disable_web_page_preview":True},
            timeout=15
        )
    except Exception as e:
        print("Telegram error:", e)

# ----- Saqar -----
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "").strip()

# ----- Auto-scan -----
AUTO_SCAN_ENABLED   = int(os.getenv("AUTO_SCAN_ENABLED", "1"))
AUTO_PERIOD_SEC     = int(os.getenv("AUTO_PERIOD_SEC", "60"))        # ← حسب اختيارك
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "240"))   # تبريد لكل عملة
TTL_SEC             = int(os.getenv("TTL_SEC", "60"))
_LAST_SIGNAL_TS = {}

# ========= Helpers =========
def make_exchange(name): return getattr(ccxt, name)({"enableRateLimit": True})
def now_ms(): return int(time.time() * 1000)
def pct(a,b):
    try: return (a-b)/b*100.0 if b not in (0,None) else float("nan")
    except: return float("nan")
def diplomatic_sleep(ms): time.sleep(ms/1000.0)
def safe(x,d=float("nan")):
    try: return float(x)
    except: return d

def _can_signal(base): return (time.time()-_LAST_SIGNAL_TS.get(base,0))>=SIGNAL_COOLDOWN_SEC
def _mark(base): _LAST_SIGNAL_TS[base]=time.time()

def send_saqar(base, score=None, meta=None):
    if not SAQAR_WEBHOOK:
        tg_send_text("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    payload = {"cmd":"buy","coin":base}
    if score is not None: payload["confidence"] = float(score)
    if meta: payload["meta"] = meta
    try:
        r=requests.post(SAQAR_WEBHOOK.rstrip("/")+"/hook",json=payload,timeout=8)
        tg_send_text(f"📡 صقر ← buy {base} | status={r.status_code} | resp=`{(r.text or '')[:200]}`")
        return 200<=r.status_code<300
    except Exception as e:
        tg_send_text(f"❌ فشل إرسال لصقر: `{e}`"); return False

# ========= Universe: Top EUR by 24h quote volume =========
def list_quote_markets(ex, quote="EUR", top_n=TOP_UNIVERSE):
    markets = ex.load_markets()
    try:
        tix = ex.fetch_tickers()
    except Exception as e:
        tg_send_text(f"🔸 fetch_tickers fallback: {e}")
        out = [m for m,info in markets.items() if info.get("active",True) and info.get("quote")==quote]
        return out[:top_n]

    rows = []
    for sym, info in markets.items():
        if not info.get("active", True) or info.get("quote") != quote:
            continue
        tk = tix.get(sym) or {}
        last = tk.get("last") or tk.get("close")
        base_vol = tk.get("baseVolume") or (tk.get("info", {}) or {}).get("volume")
        try:
            qvol = float(base_vol) * float(last) if base_vol and last else 0.0
        except:
            qvol = 0.0
        rows.append((sym, qvol))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in rows[:top_n]]

# ========= Orderbook / Spread / OB Imbalance =========
def get_ob(ex, sym, depth=25):
    try:
        ob = ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        bid = safe(ob["bids"][0][0]); ask = safe(ob["asks"][0][0])
        if bid<=0 or ask<=0: return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        spread = (ask - bid)/((ask + bid)/2) * 10000.0
        bidvol = sum(safe(p[1], 0.0) for p in ob["bids"][:5])
        askvol = sum(safe(p[1], 0.0) for p in ob["asks"][:5])
        return {"spread_bp": spread, "bid_imb": (bidvol / max(askvol, 1e-9))}
    except Exception:
        return {"spread_bp": float("nan"), "bid_imb": float("nan")}

# ========= OHLCV helpers =========
def fetch_ohlcv_safe(ex, sym, tf, limit):
    try:
        return ex.fetch_ohlcv(sym, tf, limit=limit)
    except Exception:
        return []

def vol_eur_from_ohlcv(ohlcv, last_price):
    # sum(volume) * approximate price
    try:
        total_base = sum(safe(r[5],0.0) for r in ohlcv)
        return float(total_base) * float(last_price or 0.0)
    except Exception:
        return 0.0

# ========= Flow (buy_take_ratio) =========
def get_flow(ex, sym, sec=120):
    try:
        trs = ex.fetch_trades(sym, since=now_ms() - sec*1000, limit=200)
        if not trs: return {"buy_take_ratio": float("nan")}
        def amt(t): return safe(t.get("amount", t.get("size", 0.0)), 0.0)
        bv  = sum(amt(t) for t in trs if (t.get("side") or "").lower()=="buy")
        tot = sum(amt(t) for t in trs)
        return {"buy_take_ratio": bv / max(tot, 1e-9)}
    except Exception:
        return {"buy_take_ratio": float("nan")}

# ========= Oscillation Engine (ساعة واحدة) =========
def detect_cycles_1h(closes, threshold_pct):
    """
    يكتشف دورات ±threshold٪ على سلسلة 1m closes خلال ~60 دقيقة.
    يرجّع:
      cycles_count, median_amplitude_pct, median_duration_min, max_1m_pump
    """
    n = len(closes)
    if n < 20: 
        return 0, float("nan"), float("nan"), float("nan")

    thr = threshold_pct / 100.0
    piv_price = closes[0]
    piv_idx   = 0
    direction = 0   # 0 = غير محدد، 1 = صاعد يبحث هبوط، -1 = هابط يبحث صعود
    swings = []     # (amp_pct, duration_min)

    max_1m = 0.0
    for i in range(1, n):
        # أكبر شمعة 1m (pump guard)
        ch = abs((closes[i] - closes[i-1]) / max(closes[i-1], 1e-12)) * 100.0
        if math.isfinite(ch): max_1m = max(max_1m, ch)

        move = (closes[i] - piv_price) / max(piv_price, 1e-12)
        if direction == 0:
            # حدد الاتجاه الأول إذا تجاوزنا الثابت
            if move >=  thr: direction = 1
            if move <= -thr: direction = -1
        elif direction == 1:
            # كنا طالعين: هل هبطنا بما يكفي لنغلق دورة؟
            if move <= -thr:
                amp_up   = (max(closes[piv_idx:i+1]) - piv_price) / max(piv_price, 1e-12)
                amp_down = abs(move)
                amp = (abs(amp_up) + abs(amp_down)) * 50.0  # تقريب لسعة الدورة بالمئة
                dur = (i - piv_idx)
                swings.append( (amp, dur) )
                piv_price = closes[i]; piv_idx = i; direction = -1
        elif direction == -1:
            # كنا نازلين: هل صعدنا بما يكفي لنغلق دورة؟
            if move >= thr:
                amp_down = (piv_price - min(closes[piv_idx:i+1])) / max(piv_price, 1e-12)
                amp_up   = abs(move)
                amp = (abs(amp_down) + abs(amp_up)) * 50.0
                dur = (i - piv_idx)
                swings.append( (amp, dur) )
                piv_price = closes[i]; piv_idx = i; direction = 1

    if not swings:
        return 0, float("nan"), float("nan"), max_1m

    amps = [a for a,_ in swings if math.isfinite(a)]
    durs = [d for _,d in swings if d>0]
    med_amp = st.median(amps) if amps else float("nan")
    med_dur = st.median(durs) if durs else float("nan")
    return len(swings), med_amp, med_dur, max_1m

def osc_score(cycles_hr, median_amp_pct, median_dur_min, bid_imb, spread_bp, flow_ratio):
    # نقاط: أعلى عدد دورات وسعة أكبر ومدد أقصر + OB أفضل + سبريد أقل + فلو أعلى
    w1, w2, w3, w4, w5, w6 = 0.9, 0.8, 0.5, 0.3, 0.4, 0.3
    def norm(x, lo, hi):
        if not math.isfinite(x): return 0.0
        return max(0.0, min(1.0, (x - lo) / max(hi - lo, 1e-9)))
    def clamp(x, lo, hi):
        if not math.isfinite(x): return 0.0
        return max(lo, min(hi, x))
    s = 0.0
    s += w1 * norm(cycles_hr, 0, 20)
    s += w2 * norm(median_amp_pct, 0.3, 1.5)
    s -= w3 * norm(median_dur_min, 10.0, 1.0)  # عكسي: الأقصر أفضل
    s += w4 * norm(clamp(bid_imb,1.0,2.0), 1.0, 2.0)
    s -= w5 * norm(spread_bp, 15.0, 60.0)
    s += w6 * norm(flow_ratio, 0.5, 0.8)
    return s

# ========= Scan (Oscillation Top-1) =========
def scan_oscillation_top1():
    ex = make_exchange(EXCHANGE)
    syms = list_quote_markets(ex, QUOTE, top_n=TOP_UNIVERSE)

    # A) فلتر أولي: السبريد وميزان الـOB (خفيف وسريع)
    prelim = []
    for sym in syms[:OB_PRE_TOP_N]:
        try:
            ob = get_ob(ex, sym)
            ok_spread = math.isfinite(ob["spread_bp"]) and ob["spread_bp"] <= MAX_SPREAD_BP
            if not ok_spread:
                continue
            prelim.append({"symbol": sym, **ob})
        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)

    if not prelim:
        return pd.DataFrame()

    df = pd.DataFrame(prelim)

    # B) فلتر سيولة 5m + حماية الضخ
    rows_b = []
    for r in df.to_dict("records")[:VOL_PRE_TOP_N]:
        sym = r["symbol"]
        o5 = fetch_ohlcv_safe(ex, sym, "5m", 13)  # ~ساعة
        if not o5 or len(o5) < 13: 
            continue
        closes5 = [safe(x[4]) for x in o5]
        last_p  = closes5[-1]
        vol5_eur = vol_eur_from_ohlcv(o5, last_p)
        # أبسط تقدير حجم 1m من آخر شمعتين 5m
        vol1_eur = safe(o5[-1][5],0.0)*last_p/5.0
        pump_guard = max([abs(pct(o5[i][4], o5[i-1][4])) for i in range(1,len(o5))] or [0.0])

        if vol5_eur < VOL5M_MIN_EUR or vol1_eur < VOL1M_MIN_EUR:
            continue
        if pump_guard > PUMP_CAP_1M_PCT:
            continue

        rows_b.append({**r, "last_price": last_p, "vol5m_eur": vol5_eur, "est_vol1m_eur": vol1_eur})
        diplomatic_sleep(REQUEST_SLEEP_MS)

    if not rows_b:
        return pd.DataFrame()

    df_b = pd.DataFrame(rows_b)

    # C) حساب الدورات من 1m + (flow للتوب فقط لتقليل الكلفة)
    candidates = []
    # لنحسب flow فقط لأفضل spread وimb
    flow_syms = set(df_b.sort_values(by=["spread_bp","bid_imb"], ascending=[True,False]).head(FLOW_TOP_N)["symbol"].tolist())

    for r in df_b.to_dict("records"):
        sym = r["symbol"]
        o1 = fetch_ohlcv_safe(ex, sym, "1m", 70)  # ~60 دقيقة + هامش
        if not o1 or len(o1) < 50:
            continue
        closes1 = [safe(x[4]) for x in o1][-61:]  # آخر 61 قيمة
        cycles, med_amp, med_dur, max_1m = detect_cycles_1h(closes1, threshold_pct=CAPTURE_MIN_PCT)
        # تأكيد قابلية الالتقاط: أمبليتود >= 0.6% + تكلفة (رسوم/سبريد) — بسيطة: نطلب فقط ≥ 0.6%
        if not math.isfinite(med_amp) or med_amp < CAPTURE_MIN_PCT:
            continue

        flow_ratio = float("nan")
        if sym in flow_syms:
            flow_ratio = get_flow(ex, sym, sec=120)["buy_take_ratio"]
            diplomatic_sleep(REQUEST_SLEEP_MS)

        score = osc_score(cycles, med_amp, med_dur, r["bid_imb"], r["spread_bp"], flow_ratio)

        candidates.append({
            "symbol": sym,
            "cycles_hr": cycles,
            "median_amp_pct": med_amp,
            "median_dur_min": med_dur,
            "spread_bp": r["spread_bp"],
            "bid_imb": r["bid_imb"],
            "flow_ratio": flow_ratio,
            "score": score
        })
        diplomatic_sleep(REQUEST_SLEEP_MS)

    if not candidates:
        return pd.DataFrame()

    df_c = pd.DataFrame(candidates)
    # شروط نهائية صلبة
    filt = (df_c["cycles_hr"] >= 6) & (df_c["median_amp_pct"] >= CAPTURE_MIN_PCT) & (df_c["score"] >= OSC_THRESHOLD)
    df_c = df_c[filt]
    if not len(df_c):
        return pd.DataFrame()
    df_c.sort_values(by=["score","cycles_hr","median_amp_pct","bid_imb"], ascending=[False,False,False,False], inplace=True)
    return df_c.head(1).reset_index(drop=True)

# ========= Run =========
def run_and_report(chat=None):
    try:
        if MODE != "oscillation":
            tg_send_text(f"ℹ️ MODE='{MODE}' غير مفعّل هنا. سأستخدم oscillation.", chat)

        df = scan_oscillation_top1()
        if df is None or not len(df):
            tg_send_text("⚠️ لا مرشح صالح (ساعة/ذبذبة).", chat); return

        cand = df.iloc[0].to_dict()
        base = cand["symbol"].split("/")[0]
        msg = (
            f"🔄 *Top1:* {cand['symbol']}\n"
            f"cycles={cand['cycles_hr']} | amp≈{cand['median_amp_pct']:.2f}% | dur≈{(cand['median_dur_min'] or float('nan')):.1f}m\n"
            f"spread={cand['spread_bp']:.0f}bp ob={cand['bid_imb']:.2f} flow={cand['flow_ratio'] if math.isfinite(cand['flow_ratio']) else '—'}\n"
            f"score={cand['score']:.2f}  ✅"
        )
        tg_send_text(msg, chat)

        if _can_signal(base) and send_saqar(base, score=cand["score"], meta={
            "cycles": int(cand["cycles_hr"]),
            "amp_pct": round(cand["median_amp_pct"], 3),
            "dur_min": float(cand["median_dur_min"] or 0),
            "spread_bp": round(cand["spread_bp"],1),
            "bid_imb": round(cand["bid_imb"],2),
            "flow": (float(cand["flow_ratio"]) if math.isfinite(cand["flow_ratio"]) else None),
            "ttl": TTL_SEC
        }):
            _mark(base)
        else:
            tg_send_text("⏸ تم تجاوز الإرسال (cooldown أو فشل).", chat)

    except ccxt.BaseError as e:
        tg_send_text(f"🐞 bitvavo/ccxt:\n{getattr(e,'__dict__',{}) or str(e)}", chat)
    except Exception as e:
        tg_send_text(f"🐞 runandreport: {e}", chat)

# ========= Auto Loop =========
def auto_scan_loop():
    if not AUTO_SCAN_ENABLED: 
        tg_send_text("🤖 Auto-Scan معطّل.")
        return
    tg_send_text(f"🤖 Auto-Scan كل {AUTO_PERIOD_SEC}s | Mode=Oscillation | Top{TOP_UNIVERSE} {QUOTE}")
    while True:
        try: run_and_report()
        except Exception as e: tg_send_text(f"🐞 AutoScan error: `{e}`")
        time.sleep(max(30, AUTO_PERIOD_SEC))

# ========= HTTP =========
@app.route("/", methods=["GET"])
def health(): return "ok", 200

# webhook تيليغرام: /scan
@app.route("/webhook", methods=["POST"])
def tg_webhook():
    try:
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", CHAT_ID))
        if text.startswith("/scan"):
            tg_send_text("⏳ بدأ فحص Oscillation بالخلفية…", chat_id)
            Thread(target=run_and_report, args=(chat_id,), daemon=True).start()
        else:
            tg_send_text("أوامر: /scan", chat_id)
        return jsonify(ok=True), 200
    except Exception as e:
        print("Webhook error:", e)
        return jsonify(ok=True), 200

# مسار اختبار يدوي لإرسال لصقر
@app.route("/test_buy", methods=["GET"])
def test_buy():
    coin = (request.args.get("coin") or "ADA").upper()
    ok = send_saqar(coin, score=2.0, meta={"ttl":TTL_SEC})
    return jsonify(ok=ok, coin=coin), (200 if ok else 500)

# ========= Main =========
if __name__ == "__main__":
    Thread(target=auto_scan_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))