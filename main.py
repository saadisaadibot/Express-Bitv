# -*- coding: utf-8 -*-
"""
Auto-Signal Scanner — Bitvavo (QUOTE=EUR by default) → Saqar /hook
- فلتر أسبوعي + فلتر ساعة + فلتر 5 دقائق (مخففة).
- فلتر Pulse (squeeze + OB + flow + vol).
- يرسل إشارة واحدة لصقر بشرط score ≥ MIN_SCORE و ≥ 2 أعلام مايكرو.
"""

import os, time, math, traceback, statistics as st
from datetime import datetime, timezone
from threading import Thread
import ccxt, pandas as pd
from tabulate import tabulate
from dotenv import load_dotenv
import requests
from flask import Flask, request, jsonify

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

# ----- إعدادات أساسية -----
EXCHANGE   = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE      = os.getenv("QUOTE", "EUR").upper()
TIMEFRAME  = os.getenv("TIMEFRAME", "1h")
DAYS       = int(os.getenv("DAYS", "7"))
MIN_WEEKLY_POP = float(os.getenv("MIN_WEEKLY_POP", "5.0"))
SIG_1H     = float(os.getenv("SIG_1H", "1.0"))
SIG_5M     = float(os.getenv("SIG_5M", "0.5"))
MAX_MARKETS= int(os.getenv("MAX_MARKETS", "300"))
REQUEST_SLEEP_MS = int(os.getenv("REQUEST_SLEEP_MS", "80"))

# ----- Pulse Layer -----
TOP_MICRO_N        = int(os.getenv("TOP_MICRO_N", "30"))
MAX_SPREAD_BP      = float(os.getenv("MAX_SPREAD_BP", "20"))
REQ_BID_IMB        = float(os.getenv("REQ_BID_IMB", "1.7"))
MIN_BUY_TAKE_RATIO = float(os.getenv("MIN_BUY_TAKE_RATIO", "0.60"))
VOL_SPIKE_PCT      = float(os.getenv("VOL_SPIKE_PCT", "70"))
SQUEEZE_PCTL       = float(os.getenv("SQUEEZE_PCTL", "35"))
MIN_SCORE          = float(os.getenv("MIN_SCORE", "3.0"))

# ----- تيليغرام -----
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()

# ----- صقر -----
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "").strip()

# ----- أوتو-سكان -----
AUTO_SCAN_ENABLED  = int(os.getenv("AUTO_SCAN_ENABLED", "1"))
AUTO_PERIOD_SEC    = int(os.getenv("AUTO_PERIOD_SEC", "180"))

SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "180"))
_LAST_SIGNAL_TS = {}

# ========= Helpers =========
def make_exchange(name): return getattr(ccxt, name)({"enableRateLimit": True})
def now_ms(): return int(datetime.now(timezone.utc).timestamp() * 1000)
def ms_days_ago(n): return now_ms() - n*24*60*60*1000
def pct(a,b): return (a-b)/b*100.0 if b not in (0,None) else float("nan")
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
        },timeout=15)
    except: pass

# ========= Market Data =========
def list_quote_markets(ex, quote="EUR"):
    markets = ex.load_markets()
    return [m for m,info in markets.items() if info.get("quote")==quote][:MAX_MARKETS]

def fetch_week_ohlcv(ex,symbol): return ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=ms_days_ago(DAYS), limit=200)

def analyze_symbol(ohlcv):
    if not ohlcv: return None
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    return {
        "weekly_low": float(df["low"].min()),
        "weekly_high": float(df["high"].max()),
        "last_close": float(df["close"].iloc[-1]),
        "prev_close": float(df["close"].iloc[-2]),
        "up_from_week_low_pct": pct(df["close"].iloc[-1], df["low"].min()),
        "last_hour_change_pct": pct(df["close"].iloc[-1], df["close"].iloc[-2])
    }

def get_5m_change_pct(ex,sym):
    o = ex.fetch_ohlcv(sym,"5m",limit=3)
    if len(o)<2: return float("nan")
    return pct(o[-1][4],o[-2][4])

# ========= Pulse Features =========
def bb_bandwidth(closes, window=20, k=2):
    if len(closes)<window: return float("nan")
    seg=closes[-window:]; mu=sum(seg)/len(seg)
    sd=(sum((c-mu)**2 for c in seg)/len(seg))**0.5
    return (2*k*sd)/mu*100 if mu>0 else float("nan")

def get_1h_squeeze(ex,sym):
    o=ex.fetch_ohlcv(sym,"1h",since=ms_days_ago(7),limit=200)
    if len(o)<40: return {"squeeze_pctl":float("nan")}
    closes=[safe(r[4]) for r in o]; bw_now=bb_bandwidth(closes)
    hist=[bb_bandwidth(closes[:i]) for i in range(20,len(closes))]
    hist=[h for h in hist if math.isfinite(h)]
    rank=sum(1 for h in hist if h<=bw_now)
    return {"squeeze_pctl": rank/len(hist)*100.0}

def get_ob(ex,sym,depth=25):
    ob=ex.fetch_order_book(sym,limit=depth)
    bid=safe(ob["bids"][0][0]); ask=safe(ob["asks"][0][0])
    spread=(ask-bid)/((ask+bid)/2)*10000
    bidvol=sum(safe(p[1]) for p in ob["bids"]); askvol=sum(safe(p[1]) for p in ob["asks"])
    return {"spread_bp":spread,"bid_imb":bidvol/max(askvol,1e-9)}

def get_flow(ex,sym,sec=120):
    trs=ex.fetch_trades(sym,since=now_ms()-sec*1000,limit=200)
    if not trs: return {"buy_take_ratio":float("nan")}
    bv=sum(safe(t["amount"]) for t in trs if t["side"]=="buy")
    tot=sum(safe(t["amount"]) for t in trs)
    return {"buy_take_ratio": bv/max(tot,1e-9)}

def get_volspike(ex,sym):
    o=ex.fetch_ohlcv(sym,"5m",limit=50)
    vols=[safe(r[5]) for r in o]; vnow=vols[-1]; med=st.median(vols[-21:-1])
    return {"vol_spike_pct": (vnow/med-1)*100 if med>0 else float("nan")}

# ========= Scan =========
def scan_once():
    ex=make_exchange(EXCHANGE)
    syms=list_quote_markets(ex,QUOTE)
    rows=[]
    for sym in syms:
        try:
            r=analyze_symbol(fetch_week_ohlcv(ex,sym))
            if not r: continue
            if r["up_from_week_low_pct"]<MIN_WEEKLY_POP: continue
            rows.append({"symbol":sym,**r})
        except: pass
        diplomatic_sleep(REQUEST_SLEEP_MS)
    df=pd.DataFrame(rows)
    if len(df):
        df["last_5m_change_pct"]=df["symbol"].map(lambda s:get_5m_change_pct(ex,s))
        df["sig_5m"]=df["last_5m_change_pct"]>=SIG_5M
        df["sig_last_hour"]=df["last_hour_change_pct"]>=SIG_1H
    return df

# ========= Candidate Picker =========
def enrich_pulse(df):
    if not len(df): return df
    ex=make_exchange(EXCHANGE)
    top=df.head(TOP_MICRO_N).to_dict("records")
    out=[]
    for r in top:
        sym=r["symbol"]
        sq=get_1h_squeeze(ex,sym); ob=get_ob(ex,sym); fl=get_flow(ex,sym); vs=get_volspike(ex,sym)
        score=0; flags=0
        if sq["squeeze_pctl"]<=SQUEEZE_PCTL: score+=1; flags+=1
        if ob["spread_bp"]<=MAX_SPREAD_BP and ob["bid_imb"]>=REQ_BID_IMB: score+=1.6; flags+=1
        if fl["buy_take_ratio"]>=MIN_BUY_TAKE_RATIO: score+=0.9; flags+=1
        if vs["vol_spike_pct"]>=VOL_SPIKE_PCT: score+=0.9; flags+=1
        r.update(sq); r.update(ob); r.update(fl); r.update(vs)
        r["pulse_score"]=score; r["pulse_flags"]=flags
        out.append(r)
    return pd.DataFrame(out)

def pick_best(df):
    if df is None or not len(df): return None
    cand=df[(df["sig_5m"]) & (df["sig_last_hour"]) & (df["pulse_score"]>=MIN_SCORE) & (df["pulse_flags"]>=2)]
    if not len(cand): return None
    cand=cand.sort_values(by=["pulse_score","last_5m_change_pct","last_hour_change_pct"],ascending=False)
    return cand.iloc[0].to_dict()

# ========= Saqar Bridge =========
def _can_signal(base): return (time.time()-_LAST_SIGNAL_TS.get(base,0))>=SIGNAL_COOLDOWN_SEC
def _mark(base): _LAST_SIGNAL_TS[base]=time.time()
def send_saqar(base):
    if not SAQAR_WEBHOOK: return False
    try:
        r=requests.post(SAQAR_WEBHOOK.rstrip("/")+"/hook",json={"cmd":"buy","coin":base},timeout=8)
        return r.status_code==200
    except: return False

# ========= Run =========
def run_and_report(chat=None):
    df=scan_once()
    if not len(df):
        tg_send_text("⚠️ لا نتائج",chat); return
    micro=enrich_pulse(df)
    best=pick_best(micro)
    if best and _can_signal(best["symbol"].split("/")[0]):
        tg_send_text(f"🧠 مرشح: *{best['symbol']}* | score={best['pulse_score']:.1f} flags={best['pulse_flags']}")
        if send_saqar(best["symbol"].split("/")[0]): _mark(best["symbol"].split("/")[0])

# ========= Auto Loop =========
def auto_scan_loop():
    if not AUTO_SCAN_ENABLED: return
    while True:
        try: run_and_report()
        except Exception as e: tg_send_text(f"🐞 {e}")
        time.sleep(max(30,AUTO_PERIOD_SEC))

# ========= HTTP =========
@app.route("/",methods=["GET"])
def health(): return "ok",200

@app.route("/webhook",methods=["POST"])
def tg_webhook():
    upd=request.json or {}; txt=(upd.get("message") or{}).get("text","")
    chat_id=str((upd.get("message")or{}).get("chat",{}).get("id",CHAT_ID))
    if txt.startswith("/scan"): run_and_report(chat_id)
    return jsonify(ok=True)

# ========= Main =========
if __name__=="__main__":
    Thread(target=auto_scan_loop,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))