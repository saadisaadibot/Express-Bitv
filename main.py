# -*- coding: utf-8 -*-
"""
Auto-Signal Scanner — Pulse-First (Bitvavo EUR → Saqar)
- يمسح السوق مباشرةً بالـPulse (OB + Flow + Vol + Squeeze).
- يختار أعلى pulse_score ويرسل شراء لصقر.
- Top 150 أزواج EUR حسب حجم التداول (24h) عبر fetch_tickers().
- كاش لسكيز 1h (تحديث كل SQUEEZE_REFRESH_MIN دقيقة).
- ويبهوك تيليغرام يرد فورًا (منفذ الخلفية Thread لتفادي 499).
- إعدادات محافظة لتجنّب rate-limit.
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
REQUEST_SLEEP_MS      = int(os.getenv("REQUEST_SLEEP_MS", "150"))  # زوّدناه لتخفيف الضغط

# ----- Pulse thresholds -----
MAX_SPREAD_BP      = float(os.getenv("MAX_SPREAD_BP", "50"))
REQ_BID_IMB        = float(os.getenv("REQ_BID_IMB", "1.4"))
MIN_BUY_TAKE_RATIO = float(os.getenv("MIN_BUY_TAKE_RATIO", "0.55"))
VOL_SPIKE_PCT      = float(os.getenv("VOL_SPIKE_PCT", "50"))
SQUEEZE_PCTL       = float(os.getenv("SQUEEZE_PCTL", "40"))
MIN_SCORE          = float(os.getenv("MIN_SCORE", "1.0"))

# مراحل الفرز لتقليل الريكوستات
OB_FLOW_TOP_N = int(os.getenv("OB_FLOW_TOP_N","100"))
VOL5M_TOP_N   = int(os.getenv("VOL5M_TOP_N","30"))
SQUEEZE_REFRESH_MIN = int(os.getenv("SQUEEZE_REFRESH_MIN","30"))

# ----- Telegram -----
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()

# ----- Saqar -----
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "").strip()

# ----- Auto-scan -----
AUTO_SCAN_ENABLED   = int(os.getenv("AUTO_SCAN_ENABLED", "1"))
AUTO_PERIOD_SEC     = int(os.getenv("AUTO_PERIOD_SEC", "300"))   # 5 دقائق افتراضيًا
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "180"))
_LAST_SIGNAL_TS = {}
_SQ_CACHE = {}  # symbol -> {"ts": epoch, "pctl": float}

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

def tg_send_text(text, chat_id=None):
    if not BOT_TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
            "chat_id": chat_id or CHAT_ID, "text": text,
            "parse_mode": "Markdown","disable_web_page_preview":True
        }, timeout=15)
    except Exception as e:
        print("Telegram error:", e)

# ========= Universe: Top 150 by EUR 24h volume =========
def list_quote_markets(ex, quote="EUR"):
    markets = ex.load_markets()
    try:
        tix = ex.fetch_tickers()  # bulk مرة واحدة
    except Exception as e:
        tg_send_text(f"🔸 fetch_tickers fallback: {e}")
        # fallback: أول 150 زوج EUR
        out = [m for m,info in markets.items() if info.get("active",True) and info.get("quote")==quote]
        return out[:150]

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
    return [sym for sym, _ in rows[:150]]

# ========= Pulse Features =========
def bb_bandwidth(closes, window=20, k=2):
    if len(closes) < window: return float("nan")
    seg = closes[-window:]; mu = sum(seg)/len(seg)
    sd = (sum((c-mu)**2 for c in seg)/len(seg))**0.5
    return (2*k*sd)/mu*100 if mu>0 else float("nan")

def get_1h_squeeze_once(ex, sym):
    o = ex.fetch_ohlcv(sym, "1h", limit=200)
    if not o or len(o) < 40: return float("nan")
    closes = [safe(r[4]) for r in o]
    bw_now = bb_bandwidth(closes)
    hist = [bb_bandwidth(closes[:i]) for i in range(20, len(closes))]
    hist = [h for h in hist if math.isfinite(h)]
    if not hist or not math.isfinite(bw_now): return float("nan")
    rank = sum(1 for h in hist if h <= bw_now)
    return rank/len(hist)*100.0

def get_squeeze_cached(ex, sym):
    now = time.time()
    item = _SQ_CACHE.get(sym)
    if item and (now - item["ts"]) < SQUEEZE_REFRESH_MIN*60:
        return item["pctl"]
    pctl = get_1h_squeeze_once(ex, sym)
    _SQ_CACHE[sym] = {"ts": now, "pctl": pctl}
    return pctl

def get_ob(ex, sym, depth=25):
    try:
        ob = ex.fetch_order_book(sym, limit=depth)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        bid = safe(ob["bids"][0][0]); ask = safe(ob["asks"][0][0])
        if bid<=0 or ask<=0: return {"spread_bp": float("nan"), "bid_imb": float("nan")}
        spread = (ask - bid)/((ask + bid)/2) * 10000.0
        bidvol = sum(safe(p[1], 0.0) for p in ob["bids"])
        askvol = sum(safe(p[1], 0.0) for p in ob["asks"])
        return {"spread_bp": spread, "bid_imb": bidvol / max(askvol, 1e-9)}
    except Exception:
        return {"spread_bp": float("nan"), "bid_imb": float("nan")}

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

def get_volspike(ex, sym):
    try:
        o = ex.fetch_ohlcv(sym, "5m", limit=50)
        if not o or len(o) < 25: return {"vol_spike_pct": float("nan")}
        vols = [safe(r[5],0.0) for r in o]
        vnow = vols[-1]; med = st.median(vols[-21:-1])
        if med <= 0: return {"vol_spike_pct": float("nan")}
        return {"vol_spike_pct": (vnow/med - 1.0)*100.0}
    except Exception:
        return {"vol_spike_pct": float("nan")}

def get_5m_change_pct(ex, sym):
    try:
        o = ex.fetch_ohlcv(sym, "5m", limit=3)
        if not o or len(o) < 2: return float("nan")
        return pct(o[-1][4], o[-2][4])
    except Exception:
        return float("nan")

# ========= Pulse-first Scan =========
def scan_pulse_first():
    ex = make_exchange(EXCHANGE)
    syms = list_quote_markets(ex, QUOTE)

    # A) OB + Flow (لكل الـ150)
    prelim = []
    for sym in syms:
        try:
            ob = get_ob(ex, sym)
            fl = get_flow(ex, sym)
            ob_ok = math.isfinite(ob["spread_bp"]) and ob["spread_bp"] <= MAX_SPREAD_BP and \
                    math.isfinite(ob["bid_imb"]) and ob["bid_imb"] >= REQ_BID_IMB
            fl_ok = math.isfinite(fl["buy_take_ratio"]) and fl["buy_take_ratio"] >= MIN_BUY_TAKE_RATIO
            pre_score = (1.6 if ob_ok else 0) + (0.9 if fl_ok else 0)
            prelim.append({"symbol": sym, **ob, **fl, "pre_score": pre_score, "pre_flags": int(ob_ok)+int(fl_ok)})
        except Exception:
            pass
        diplomatic_sleep(REQUEST_SLEEP_MS)

    df = pd.DataFrame(prelim)
    if not len(df): return pd.DataFrame()

    # B) Volume spike + 5m change (لأفضل OB_FLOW_TOP_N)
    df.sort_values(by=["pre_score","bid_imb"], ascending=False, inplace=True)
    df = df.head(min(OB_FLOW_TOP_N, len(df))).reset_index(drop=True)
    df["vol_spike_pct"] = df["symbol"].map(lambda s: get_volspike(ex, s)["vol_spike_pct"])
    df["last_5m_change_pct"] = df["symbol"].map(lambda s: get_5m_change_pct(ex, s))

    # C) Squeeze (من الكاش) لأفضل VOL5M_TOP_N
    df.sort_values(by=["pre_score","vol_spike_pct","last_5m_change_pct"], ascending=False, inplace=True)
    df = df.head(min(VOL5M_TOP_N, len(df))).reset_index(drop=True)
    df["squeeze_pctl"] = df["symbol"].map(lambda s: get_squeeze_cached(ex, s))

    # إجمالي السكور
    rows = []
    for r in df.to_dict("records"):
        score = r["pre_score"]; flags = r["pre_flags"]
        if math.isfinite(r.get("vol_spike_pct",float("nan"))) and r["vol_spike_pct"] >= VOL_SPIKE_PCT:
            score += 0.9; flags += 1
        if math.isfinite(r.get("squeeze_pctl",float("nan"))) and r["squeeze_pctl"] <= SQUEEZE_PCTL:
            score += 1.0; flags += 1
        r["pulse_score"] = score; r["pulse_flags"] = flags
        rows.append(r)
    return pd.DataFrame(rows)

# ========= Saqar Bridge =========
def _can_signal(base): return (time.time()-_LAST_SIGNAL_TS.get(base,0))>=SIGNAL_COOLDOWN_SEC
def _mark(base): _LAST_SIGNAL_TS[base]=time.time()
def send_saqar(base):
    if not SAQAR_WEBHOOK:
        tg_send_text("⚠️ SAQAR_WEBHOOK غير مضبوط."); return False
    try:
        r=requests.post(SAQAR_WEBHOOK.rstrip("/")+"/hook",json={"cmd":"buy","coin":base},timeout=8)
        tg_send_text(f"📡 صقر ← buy {base} | status={r.status_code} | resp=`{(r.text or '')[:200]}`")
        return 200<=r.status_code<300
    except Exception as e:
        tg_send_text(f"❌ فشل إرسال لصقر: `{e}`"); return False

# ========= Run =========
def run_and_report(chat=None):
    try:
        df = scan_pulse_first()
        if not len(df):
            tg_send_text("ℹ️ لا مرشح Pulse.", chat); return
        cand = df[df["pulse_score"]>=MIN_SCORE].sort_values(
            by=["pulse_score","last_5m_change_pct","bid_imb"], ascending=False
        ).head(1).iloc[0].to_dict()
        base = cand["symbol"].split("/")[0]
        tg_send_text(
            f"🧠 مرشح: *{cand['symbol']}* | sc={cand['pulse_score']:.1f} fl={cand['pulse_flags']} | "
            f"SQ≤{SQUEEZE_PCTL}:{'✅' if math.isfinite(cand.get('squeeze_pctl',float('nan'))) and cand['squeeze_pctl']<=SQUEEZE_PCTL else '❌'} "
            f"OB:{'✅' if cand.get('pre_score',0)>=1.6 else '❌'} "
            f"FL:{'✅' if cand.get('pre_flags',0)>=1 else '❌'} "
            f"VOL≥{VOL_SPIKE_PCT}:{'✅' if math.isfinite(cand.get('vol_spike_pct',float('nan'))) and cand['vol_spike_pct']>=VOL_SPIKE_PCT else '❌'}",
            chat
        )
        if _can_signal(base) and send_saqar(base): _mark(base)
    except ccxt.BaseError as e:
        # لو انحظرنا أو تجاوزنا limit
        tg_send_text(f"🐞 runandreport:bitvavo\n{getattr(e,'__dict__',{}) or str(e)}", chat)
    except Exception as e:
        tg_send_text(f"🐞 runandreport:{e}", chat)

# ========= Auto Loop =========
def auto_scan_loop():
    if not AUTO_SCAN_ENABLED: return
    tg_send_text(f"🤖 Auto-Scan كل {AUTO_PERIOD_SEC}s | Pulse-first | Top150 EUR")
    while True:
        try: run_and_report()
        except Exception as e: tg_send_text(f"🐞 AutoScan error: `{e}`")
        time.sleep(max(30, AUTO_PERIOD_SEC))

# ========= HTTP =========
@app.route("/", methods=["GET"])
def health(): return "ok", 200

# يرد فورًا ويشغّل الفحص بالخلفية (لمنع 499)
@app.route("/webhook", methods=["POST"])
def tg_webhook():
    try:
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", CHAT_ID))
        if text.startswith("/scan"):
            tg_send_text("⏳ بدأ الفحص بالخلفية…", chat_id)
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
    ok = send_saqar(coin)
    return jsonify(ok=ok, coin=coin), (200 if ok else 500)

# ========= Main =========
if __name__ == "__main__":
    Thread(target=auto_scan_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))