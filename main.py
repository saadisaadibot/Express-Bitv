# -*- coding: utf-8 -*-
import os, time, math, traceback
from datetime import datetime, timezone
import ccxt, pandas as pd
from tabulate import tabulate
from dotenv import load_dotenv
import requests
from flask import Flask, request, jsonify

load_dotenv()

EXCHANGE         = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE            = os.getenv("QUOTE", "EUR").upper()
TIMEFRAME        = os.getenv("TIMEFRAME", "1h")
DAYS             = int(os.getenv("DAYS", "7"))
MIN_WEEKLY_POP   = float(os.getenv("MIN_WEEKLY_POP", "10.0"))
SIG_1H           = float(os.getenv("SIG_1H", "2.0"))
MAX_MARKETS      = int(os.getenv("MAX_MARKETS", "300"))
REQUEST_SLEEP_MS = int(os.getenv("REQUEST_SLEEP_MS", "80"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()
PORT      = int(os.getenv("PORT", "8080"))

# === صقر (الشراء) ===
SAQAR_WEBHOOK       = os.getenv("SAQAR_WEBHOOK", "").strip()
SAQAR_MODE          = os.getenv("SAQAR_MODE", "text").strip().lower()  # text | json
SAQAR_SIGNATURE     = os.getenv("SAQAR_SIGNATURE", "")                 # اتركه فاضي إذا هيك صقر بدو
SAQAR_COOLDOWN_SEC  = int(os.getenv("SAQAR_COOLDOWN_SEC", "120"))

# ذاكرة بسيطة لمنع التكرار لكل عملة
_LAST_SIGNAL_TS = {}

app = Flask(__name__)

def make_exchange(name: str):
    if not hasattr(ccxt, name): raise ValueError(f"Exchange '{name}' غير مدعوم.")
    return getattr(ccxt, name)({"enableRateLimit": True})

def now_ms(): return int(datetime.now(timezone.utc).timestamp() * 1000)
def ms_days_ago(n: int): return now_ms() - n*24*60*60*1000
def pct(a,b): 
    try: return (a-b)/b*100.0 if b not in (0,None) else float("nan")
    except: return float("nan")
def diplomatic_sleep(ms:int): time.sleep(ms/1000.0)

def list_quote_markets(ex, quote="EUR"):
    markets = ex.load_markets(); syms=[]
    for m,info in markets.items():
        if info.get("active",True) and info.get("quote")==quote:
            syms.append(m); 
            if len(syms)>=MAX_MARKETS: break
    if not syms: raise RuntimeError(f"لا توجد أسواق {quote}. جرّب QUOTE=USDT.")
    return syms

def fetch_week_ohlcv(ex, symbol, timeframe="1h", days=7):
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, since=ms_days_ago(days), limit=200)

def analyze_symbol(ohlcv):
    if not ohlcv or len(ohlcv)<5: return None
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    weekly_low, weekly_high = float(df["low"].min()), float(df["high"].max())
    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2]) if len(df)>=2 else float("nan")
    return {
        "weekly_low": weekly_low, "weekly_high": weekly_high,
        "last_close": last_close, "prev_close": prev_close,
        "up_from_week_low_pct": pct(last_close, weekly_low),
        "last_hour_change_pct": pct(last_close, prev_close),
        "samples": len(df), "last_ts": int(df["ts"].iloc[-1]),
    }

def scan_once():
    ex = make_exchange(EXCHANGE)
    symbols = list_quote_markets(ex, QUOTE)
    rows, errors = [], []
    for sym in symbols:
        try:
            res = analyze_symbol(fetch_week_ohlcv(ex, sym, TIMEFRAME, DAYS))
            if not res: continue
            if math.isnan(res["up_from_week_low_pct"]) or res["up_from_week_low_pct"] < MIN_WEEKLY_POP: 
                continue
            rows.append({
                "symbol": sym, **res,
                "sig_last_hour": (res["last_hour_change_pct"] if not math.isnan(res["last_hour_change_pct"]) else -999) >= SIG_1H
            })
        except Exception as e:
            errors.append((sym, str(e)))
        finally:
            diplomatic_sleep(REQUEST_SLEEP_MS)
    df = pd.DataFrame(rows)
    if len(df): df.sort_values(by=["up_from_week_low_pct","last_hour_change_pct"], ascending=[False,False], inplace=True)
    return df, errors

def tg_send_text(text: str, chat_id: str=None, disable_web_page_preview=True):
    if not BOT_TOKEN or not (chat_id or CHAT_ID): return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id or CHAT_ID, "text": text,
            "parse_mode": "Markdown", "disable_web_page_preview": disable_web_page_preview
        }, timeout=15)
    except Exception as e:
        print("Telegram send error:", e)

def df_to_md(df: pd.DataFrame, cols):
    from tabulate import tabulate
    view = df[cols].copy()
    for c in ["last_close","weekly_low","weekly_high"]:
        if c in view.columns: view[c] = view[c].map(lambda x: round(float(x), 8))
    for c in ["up_from_week_low_pct","last_hour_change_pct"]:
        if c in view.columns: view[c] = view[c].map(lambda x: round(float(x), 3))
    return "```\n" + tabulate(view, headers="keys", tablefmt="github", showindex=False) + "\n```"

def chunk_and_send(md_text: str, header=None):
    CHUNK=3800
    if header: tg_send_text(header)
    if not md_text: return
    for i in range(0, len(md_text), CHUNK):
        tg_send_text(md_text[i:i+CHUNK])

# ====== صقر: إرسال إشارة شراء ======
def _sym_to_base(sym: str) -> str:
    # "ADA/EUR" -> "ADA"
    return sym.split("/")[0].strip().upper()

def _can_signal(symbol_base: str) -> bool:
    now = time.time()
    ts = _LAST_SIGNAL_TS.get(symbol_base, 0)
    return (now - ts) >= SAQAR_COOLDOWN_SEC

def _mark_signaled(symbol_base: str):
    _LAST_SIGNAL_TS[symbol_base] = time.time()

def send_to_saqar_text(symbol_base: str, reason: str = "", price: float = None):
    if not SAQAR_WEBHOOK: 
        tg_send_text(f"⚠️ SAQAR_WEBHOOK غير مضبوط. تجاهلت إشارة {symbol_base}.")
        return False
    payload = {
        "text": f"اشتري {symbol_base}",
        "signature": SAQAR_SIGNATURE  # اتركها "" إذا لازم توقيع فاضي
    }
    try:
        r = requests.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        ok = (200 <= r.status_code < 300)
        tg_send_text(f"📬 أرسلت لصقر: *اشتري {symbol_base}* (status={r.status_code})\nسبب: {reason}")
        return ok
    except Exception as e:
        tg_send_text(f"❌ فشل إرسال لصقر {symbol_base}: `{e}`")
        return False

def send_to_saqar_json(symbol_base: str, reason: str = "", price: float = None):
    if not SAQAR_WEBHOOK: 
        tg_send_text(f"⚠️ SAQAR_WEBHOOK غير مضبوط. تجاهلت إشارة {symbol_base}.")
        return False
    payload = {
        "coin": symbol_base,
        "quote": QUOTE,
        "action": "buy",
        "reason": reason,
        "price": price,
        "ts": int(time.time()),
        "signature": SAQAR_SIGNATURE
    }
    try:
        r = requests.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        ok = (200 <= r.status_code < 300)
        tg_send_text(f"📬 JSON لصقر: *{symbol_base}* (status={r.status_code})\nسبب: {reason}")
        return ok
    except Exception as e:
        tg_send_text(f"❌ فشل إرسال JSON لصقر {symbol_base}: `{e}`")
        return False

def maybe_signal_saqar(row):
    """
    يقرّر إذا بدنا نرسل إشارة شراء لصقر.
    شرط افتراضي: sig_last_hour=True + فوق قاع الأسبوع بما فيه الكفاية (محسوب مسبقاً).
    تقدر تضيف شروط دفتر أوامر لاحقاً إذا بتحب.
    """
    symbol = row["symbol"]             # "ADA/EUR"
    base   = _sym_to_base(symbol)      # "ADA"
    price  = float(row["last_close"])
    why    = f"{base}/{QUOTE}: ساعة {row['last_hour_change_pct']:.2f}% ، فوق قاع الأسبوع {row['up_from_week_low_pct']:.2f}%"

    if not row.get("sig_last_hour", False):
        return False

    if not _can_signal(base):
        # صامتًا: ضمن الكولداون
        return False

    ok = False
    if SAQAR_MODE == "json":
        ok = send_to_saqar_json(base, why, price)
    else:
        ok = send_to_saqar_text(base, why, price)

    if ok:
        _mark_signaled(base)
    return ok

def run_and_report(custom_chat_id=None):
    start = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    tg_send_text(f"⏳ بدء المسح — EXCHANGE={EXCHANGE} QUOTE={QUOTE} TF={TIMEFRAME} DAYS={DAYS}\n*Start:* `{start}`", custom_chat_id)
    df, errors = scan_once()
    if df is None or len(df)==0:
        tg_send_text(f"⚠️ لا توجد عملات مطابقة (≥ {MIN_WEEKLY_POP:.2f}% فوق قاع الأسبوع).", custom_chat_id)
        if errors: tg_send_text(f"ملاحظات ({len(errors)}): `{errors[0][0]} → {errors[0][1][:160]}`", custom_chat_id)
        return

    # 1) تقرير عام
    md_all = df_to_md(df, ["symbol","last_close","weekly_low","weekly_high","up_from_week_low_pct","last_hour_change_pct","samples"])
    chunk_and_send(md_all, header=f"✅ عملات ≥ *{MIN_WEEKLY_POP:.2f}%* فوق قاع الأسبوع:")

    # 2) فرص ملحوظة
    notable = df[df["sig_last_hour"]==True]
    if len(notable):
        md_notable = df_to_md(notable, ["symbol","last_close","last_hour_change_pct","up_from_week_low_pct"])
        chunk_and_send(md_notable, header=f"🚀 ارتفاع ملحوظ آخر ساعة (≥ *{SIG_1H:.2f}%*):")

        # 3) **إطلاق إشارات شراء لصقر** لكل عملة لافتة (مع كولداون)
        sent = 0
        for _, row in notable.iterrows():
            try:
                if maybe_signal_saqar(row):
                    sent += 1
            except Exception as e:
                tg_send_text(f"❌ خطأ أثناء إرسال لصقر {row.get('symbol')}: `{e}`")
        tg_send_text(f"📡 صقر: تم إرسال {sent} إشارة شراء.", custom_chat_id)
    else:
        tg_send_text(f"ℹ️ لا يوجد ارتفاع ملحوظ آخر ساعة (SIG_1H={SIG_1H:.2f}%).", custom_chat_id)

    end = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    tg_send_text(f"✅ انتهى الفحص — *End:* `{end}`", custom_chat_id)

@app.route("/", methods=["GET"])
def health(): return "ok", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        upd = request.json or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat",{}).get("id", CHAT_ID))
        if text.startswith("/start"):
            tg_send_text(
                "أهلاً! استخدم الأمر `/scan` لبدء الفحص.\n"
                "مثال: `/scan QUOTE=USDT MIN_WEEKLY_POP=12 SIG_1H=3`", chat_id)
        elif text.startswith("/scan"):
            # باراميترات سريعة من الرسالة
            try:
                for p in text.split()[1:]:
                    if "=" in p:
                        k,v=p.split("=",1); k=k.upper()
                        if k=="MIN_WEEKLY_POP": globals()["MIN_WEEKLY_POP"]=float(v)
                        elif k=="SIG_1H": globals()["SIG_1H"]=float(v)
                        elif k=="QUOTE": globals()["QUOTE"]=v.upper()
                run_and_report(chat_id)
            except Exception as e:
                tg_send_text(f"❌ خطأ: {e}", chat_id)
        else:
            tg_send_text("أوامر: `/scan` أو `/scan QUOTE=USDT MIN_WEEKLY_POP=12 SIG_1H=3`", chat_id)
        return jsonify(ok=True)
    except Exception as e:
        print("Webhook error:", e); return jsonify(ok=False), 200

if __name__ == "__main__":
    mode = os.getenv("MODE","web").lower()
    if mode=="web":
        app.run(host="0.0.0.0", port=PORT)
    else:
        run_and_report()