# -*- coding: utf-8 -*-
"""
Auto-Signal Scanner — Bitvavo (QUOTE=EUR by default) → Saqar /hook
- مسح تلقائي كل 3 دقائق (قابل للتعديل).
- فلتر أسبوعي + فلتر ساعة + فلتر 5 دقائق.
- يختار مرشّح واحد فقط ويرسل أمر شراء لصقر JSON على /hook:
    {"cmd":"buy","coin":"ADA","eur":<اختياري>}
- كولداون داخلي لمنع التكرار لكل عملة.
- أوامر تيليغرام: /scan لتشغيل فحص حالاً مع تقرير.
"""

import os, time, math, traceback
from datetime import datetime, timezone
from threading import Thread
import ccxt
import pandas as pd
from tabulate import tabulate
from dotenv import load_dotenv
import requests
from flask import Flask, request, jsonify

# ========= Boot / ENV =========
load_dotenv()
app = Flask(__name__)

# ----- مصادر البيانات -----
EXCHANGE         = os.getenv("EXCHANGE", "bitvavo").lower()
QUOTE            = os.getenv("QUOTE", "EUR").upper()
TIMEFRAME        = os.getenv("TIMEFRAME", "1h")        # للفلتر الأسبوعي/الساعة
DAYS             = int(os.getenv("DAYS", "7"))
MIN_WEEKLY_POP   = float(os.getenv("MIN_WEEKLY_POP", "10.0"))  # فوق قاع الأسبوع
SIG_1H           = float(os.getenv("SIG_1H", "2.0"))           # % آخر ساعة
SIG_5M           = float(os.getenv("SIG_5M", "1.0"))           # % آخر 5 دقائق
MAX_MARKETS      = int(os.getenv("MAX_MARKETS", "300"))
REQUEST_SLEEP_MS = int(os.getenv("REQUEST_SLEEP_MS", "80"))

# ----- إرسال تقارير تيليغرام (اختياري) -----
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()

# ----- تشغيل ويب -----
PORT      = int(os.getenv("PORT", "8080"))

# ----- صقر: ويبهوك وحيد -----
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "").strip()  # بدون /hook؛ الدالة تضيفه

# ----- أوتو-سكان -----
AUTO_SCAN_ENABLED  = int(os.getenv("AUTO_SCAN_ENABLED", "0"))   # 1 = شغال
AUTO_PERIOD_SEC    = int(os.getenv("AUTO_PERIOD_SEC", "180"))   # كل 3 دقائق

# ----- كولداون داخلي للإشارات -----
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "120"))
_LAST_SIGNAL_TS = {}  # coin_base -> ts

# ========= Helpers =========
def make_exchange(name: str):
    if not hasattr(ccxt, name):
        raise ValueError(f"Exchange '{name}' غير مدعوم.")
    return getattr(ccxt, name)({"enableRateLimit": True})

def now_ms(): return int(datetime.now(timezone.utc).timestamp() * 1000)
def ms_days_ago(n: int): return now_ms() - n*24*60*60*1000

def pct(a, b):
    try:
        return (a - b) / b * 100.0 if b not in (0, None) else float("nan")
    except:
        return float("nan")

def diplomatic_sleep(ms: int): time.sleep(ms/1000.0)

def tg_send_text(text: str, chat_id: str=None, disable_web_page_preview=True):
    if not BOT_TOKEN or not (chat_id or CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id or CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": disable_web_page_preview
        }, timeout=15)
    except Exception as e:
        print("Telegram send error:", e)

# ========= Universe =========
def list_quote_markets(ex, quote="EUR"):
    markets = ex.load_markets()
    syms = []
    for m, info in markets.items():
        if info.get("active", True) and info.get("quote") == quote:
            syms.append(m)
            if len(syms) >= MAX_MARKETS:
                break
    if not syms:
        raise RuntimeError(f"لا توجد أسواق {quote}. جرّب QUOTE=USDT.")
    return syms

# ========= Fetch / Analyze =========
def fetch_week_ohlcv(ex, symbol, timeframe="1h", days=7):
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, since=ms_days_ago(days), limit=200)

def analyze_symbol(ohlcv):
    if not ohlcv or len(ohlcv) < 5:
        return None
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    weekly_low  = float(df["low"].min())
    weekly_high = float(df["high"].max())
    last_close  = float(df["close"].iloc[-1])
    prev_close  = float(df["close"].iloc[-2]) if len(df) >= 2 else float("nan")
    return {
        "weekly_low": weekly_low,
        "weekly_high": weekly_high,
        "last_close": last_close,
        "prev_close": prev_close,
        "up_from_week_low_pct": pct(last_close, weekly_low),
        "last_hour_change_pct": pct(last_close, prev_close),
        "samples": len(df),
        "last_ts": int(df["ts"].iloc[-1]),
    }

def get_5m_change_pct(ex, symbol: str) -> float:
    """
    % التغيّر بين إغلاق آخر شمعة 5m والإغلاق الذي قبلها.
    """
    try:
        o = ex.fetch_ohlcv(symbol, timeframe="5m", limit=3)
        if not o or len(o) < 2:
            return float("nan")
        prev_close = float(o[-2][4])
        last_close = float(o[-1][4])
        if prev_close <= 0:
            return float("nan")
        return (last_close / prev_close - 1.0) * 100.0
    except Exception:
        return float("nan")

# ========= Scan =========
def scan_once():
    ex = make_exchange(EXCHANGE)
    symbols = list_quote_markets(ex, QUOTE)
    rows, errors = [], []

    # المرحلة 1: فلتر أسبوع + ساعة
    prelim = []
    for sym in symbols:
        try:
            res = analyze_symbol(fetch_week_ohlcv(ex, sym, TIMEFRAME, DAYS))
            if not res:
                continue
            if math.isnan(res["up_from_week_low_pct"]) or res["up_from_week_low_pct"] < MIN_WEEKLY_POP:
                continue
            prelim.append((sym, res))
        except Exception as e:
            errors.append((sym, str(e)))
        finally:
            diplomatic_sleep(REQUEST_SLEEP_MS)

    # رتّب لتقليل استدعاءات 5m
    prelim.sort(key=lambda x: (x[1]["up_from_week_low_pct"], x[1]["last_hour_change_pct"]), reverse=True)

    # المرحلة 2: احسب 5m لأفضل N فقط
    N = min(len(prelim), 80)
    for i, (sym, res) in enumerate(prelim):
        try:
            row = {"symbol": sym, **res}
            row["sig_last_hour"] = (res["last_hour_change_pct"] if not math.isnan(res["last_hour_change_pct"]) else -999) >= SIG_1H
            if i < N:
                c5 = get_5m_change_pct(ex, sym)
                row["last_5m_change_pct"] = c5
                row["sig_5m"] = (c5 if not math.isnan(c5) else -999) >= SIG_5M
            else:
                row["last_5m_change_pct"] = float("nan")
                row["sig_5m"] = False
            rows.append(row)
        except Exception as e:
            errors.append((sym, str(e)))

    df = pd.DataFrame(rows)
    if len(df):
        df.sort_values(
            by=["sig_5m", "sig_last_hour", "last_5m_change_pct", "last_hour_change_pct", "up_from_week_low_pct"],
            ascending=[False, False, False, False, False],
            inplace=True
        )
    return df, errors

# ========= Reporting =========
def df_to_md(df: pd.DataFrame, cols):
    view = df[cols].copy()
    for c in ["last_close","weekly_low","weekly_high"]:
        if c in view.columns:
            view[c] = view[c].map(lambda x: round(float(x), 8))
    for c in ["up_from_week_low_pct","last_hour_change_pct","last_5m_change_pct"]:
        if c in view.columns:
            view[c] = view[c].map(lambda x: round(float(x), 3))
    return "```\n" + tabulate(view, headers="keys", tablefmt="github", showindex=False) + "\n```"

def chunk_and_send(md_text: str, header=None):
    CHUNK = 3800
    if header:
        tg_send_text(header)
    if not md_text:
        return
    for i in range(0, len(md_text), CHUNK):
        tg_send_text(md_text[i:i+CHUNK])

# ========= Saqar Bridge =========
def _sym_to_base(sym: str) -> str:
    return sym.split("/")[0].strip().upper()

def _can_signal(symbol_base: str) -> bool:
    if SIGNAL_COOLDOWN_SEC <= 0:
        return True
    now = time.time()
    ts = _LAST_SIGNAL_TS.get(symbol_base, 0)
    return (now - ts) >= SIGNAL_COOLDOWN_SEC

def _mark_signaled(symbol_base: str):
    _LAST_SIGNAL_TS[symbol_base] = time.time()

def send_to_saqar_buy(coin_base: str, eur: float=None) -> bool:
    """يرسل أمر شراء لصقر كـ JSON على /hook: {"cmd":"buy","coin":"ADA","eur":25}"""
    url = (SAQAR_WEBHOOK or "").rstrip("/")
    if not url:
        tg_send_text("⚠️ SAQAR_WEBHOOK غير مضبوط.")
        return False
    url += "/hook"
    payload = {"cmd": "buy", "coin": coin_base.upper()}
    if eur is not None:
        try:
            payload["eur"] = float(eur)
        except:
            pass
    try:
        r = requests.post(url, json=payload, timeout=8)
        body = (r.text or "")[:300]
        tg_send_text(f"📡 صقر ← buy {coin_base} | status={r.status_code} | resp=`{body}`")
        return 200 <= r.status_code < 300
    except Exception as e:
        tg_send_text(f"❌ فشل إرسال لصقر: `{e}`")
        return False

def pick_best_candidate(df: pd.DataFrame):
    """
    نختار عملة واحدة فقط:
    - لازم sig_last_hour=True و sig_5m=True
    - نرتّب حسب 5m ثم 1h ثم weekly pop
    """
    if df is None or len(df) == 0:
        return None
    cand = df[(df.get("sig_last_hour", False) == True) & (df.get("sig_5m", False) == True)].copy()
    if len(cand) == 0:
        return None
    cand.sort_values(
        by=["last_5m_change_pct", "last_hour_change_pct", "up_from_week_low_pct"],
        ascending=[False, False, False],
        inplace=True
    )
    return cand.iloc[0].to_dict()

def maybe_signal_saqar(row_dict):
    if not row_dict:
        return False
    base = _sym_to_base(row_dict["symbol"])
    if not _can_signal(base):
        return False
    ok = send_to_saqar_buy(base, eur=None)  # eur=None → استخدم الرصيد في صقر
    if ok:
        _mark_signaled(base)
    return ok

# ========= One-shot run (and Telegram report) =========
def run_and_report(custom_chat_id=None):
    start = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    tg_send_text(f"⏳ بدء المسح — EXCHANGE={EXCHANGE} QUOTE={QUOTE} TF={TIMEFRAME} DAYS={DAYS}\n*Start:* `{start}`", custom_chat_id)
    df, errors = scan_once()
    if df is None or len(df) == 0:
        tg_send_text(f"⚠️ لا توجد عملات مطابقة (≥ {MIN_WEEKLY_POP:.2f}% فوق قاع الأسبوع).", custom_chat_id)
        if errors:
            tg_send_text(f"ملاحظات ({len(errors)}): `{errors[0][0]} → {errors[0][1][:160]}`", custom_chat_id)
        return

    md_all = df_to_md(df, ["symbol","last_close","last_5m_change_pct","last_hour_change_pct","up_from_week_low_pct"])
    chunk_and_send(md_all, header=f"✅ عملات ≥ *{MIN_WEEKLY_POP:.2f}%* فوق قاع الأسبوع:")

    best = pick_best_candidate(df)
    if best:
        sent = 1 if maybe_signal_saqar(best) else 0
        tg_send_text(f"📡 صقر: تم إرسال {sent} إشارة (أفضل مرشّح: {best['symbol']}).", custom_chat_id)
    else:
        tg_send_text(f"ℹ️ لا يوجد مرشّح يحقق شرط الساعة + شرط 5m (SIG_1H={SIG_1H:.2f}%, SIG_5M={SIG_5M:.2f}%).", custom_chat_id)

    end = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    tg_send_text(f"✅ انتهى الفحص — *End:* `{end}`", custom_chat_id)

# ========= Auto-scan loop =========
def auto_scan_loop():
    if not AUTO_SCAN_ENABLED:
        return
    tg_send_text(f"🤖 Auto-Scan مفعل: كل {AUTO_PERIOD_SEC}s | SIG_1H={SIG_1H:.2f}% | SIG_5M={SIG_5M:.2f}%")
    while True:
        try:
            df, _ = scan_once()
            best = pick_best_candidate(df)
            if best:
                maybe_signal_saqar(best)  # يراعي الكولداون
        except Exception as e:
            tg_send_text(f"🐞 Auto-scan error: `{e}`")
        time.sleep(max(30, AUTO_PERIOD_SEC))  # أمان: لا تقل عن 30 ثانية

# ========= HTTP =========
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        upd = request.json or {}
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", CHAT_ID))
        if text.startswith("/start"):
            tg_send_text(
                "أهلاً! استخدم الأمر `/scan` لتشغيل فحص فوري.\n"
                "مثال: `/scan QUOTE=USDT MIN_WEEKLY_POP=12 SIG_1H=3 SIG_5M=1.5`",
                chat_id
            )
        elif text.startswith("/scan"):
            try:
                # باراميترات فورية من الرسالة
                for p in text.split()[1:]:
                    if "=" in p:
                        k, v = p.split("=", 1); k = k.upper()
                        if k == "MIN_WEEKLY_POP": globals()["MIN_WEEKLY_POP"] = float(v)
                        elif k == "SIG_1H":       globals()["SIG_1H"]       = float(v)
                        elif k == "SIG_5M":       globals()["SIG_5M"]       = float(v)
                        elif k == "QUOTE":        globals()["QUOTE"]        = v.upper()
                run_and_report(chat_id)
            except Exception as e:
                tg_send_text(f"❌ خطأ: {e}", chat_id)
        else:
            tg_send_text("أوامر: `/scan` أو `/scan QUOTE=USDT MIN_WEEKLY_POP=12 SIG_1H=3 SIG_5M=1.5`", chat_id)
        return jsonify(ok=True)
    except Exception as e:
        print("Webhook error:", e)
        return jsonify(ok=False), 200

# ========= Main =========
if __name__ == "__main__":
    # شغّل اللوب التلقائي كـ daemon
    Thread(target=auto_scan_loop, daemon=True).start()

    mode = os.getenv("MODE", "web").lower()
    if mode == "web":
        app.run(host="0.0.0.0", port=PORT)
    else:
        # تشغيل فحص واحد (مفيد محلياً)
        run_and_report()