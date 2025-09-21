# -*- coding: utf-8 -*-
"""
express_main.py
مرحلي: Memory Builder (واحدي) -> Filter Brain (واحدي) -> Live Watcher (دائم)
- يقوم بالجمع التاريخي 1m لكل الأسواق المختارة (Bitvavo أو Binance عبر ccxt).
- يبني Parquet لكل عملة ويؤشر DuckDB.
- بعد اكتمال يُفلتر العملات الراكدة ويستخرج العملات التي ارتفعت >= PUMP_FROM_TROUGH% من أي قاع.
- في النهاية يبدأ مراقبة Live (Binance top-N عبر WebSocket) ويُصدر إشارات مبدئية.
- يرسل رسائل Telegram عند اكتمال كل مرحلة.
"""
import os, sys, time, math, asyncio, json, glob, traceback
from datetime import datetime, timedelta, timezone
from typing import List
import pandas as pd
import numpy as np
import duckdb
from collections import deque
from dotenv import load_dotenv

# ---- Load env ----
load_dotenv()  # from .env if present

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
CHAT_ID     = os.getenv("CHAT_ID", "")
DATA_DIR    = os.getenv("DATA_DIR", "./data")
DUCKDB_PATH = os.getenv("DUCKDB_PATH", os.path.join(DATA_DIR, "memory.duckdb"))
EXCHANGE    = os.getenv("EXCHANGE", "bitvavo").lower()
COINS_ENV   = os.getenv("COINS", "").strip()
DAYS_BACK   = int(os.getenv("DAYS_BACK", "14"))
MAX_CONC    = int(os.getenv("MAX_CONCURRENCY", "6"))
FLAT_PCT    = float(os.getenv("FLAT_PCT", "5"))
PUMP_THR    = float(os.getenv("PUMP_FROM_TROUGH", "10"))
TOPN_BINANCE= int(os.getenv("TOPN_BINANCE", "100"))
AGGRESSIVE  = int(os.getenv("AGGRESSIVE", "1"))
SAQER_HOOK  = os.getenv("SAQER_WEBHOOK", "")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))

os.makedirs(DATA_DIR, exist_ok=True)
# exchange-specific data folder
EX_DIR = os.path.join(DATA_DIR, EXCHANGE)
os.makedirs(EX_DIR, exist_ok=True)

# ---- small utilities ----
def tg_msg(text:str):
    if not (BOT_TOKEN and CHAT_ID):
        print("[TG SKIP]", text)
        return
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=12
        )
    except Exception as e:
        print("TG send error:", e)

def now_utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def utc_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

# ---- Memory Builder (async) ----
import ccxt, aiohttp

def mk_exchange():
    if EXCHANGE == "bitvavo":
        ex = ccxt.bitvavo({'enableRateLimit': True})
    else:
        ex = ccxt.binance({'enableRateLimit': True})
    ex.load_markets()
    return ex

def list_markets(ex) -> List[str]:
    if COINS_ENV:
        coins = [s.strip().upper() for s in COINS_ENV.split(",") if s.strip()]
        syms = []
        for c in coins:
            for q in ("EUR","USDT","BTC"):
                m = f"{c}/{q}"
                if m in ex.markets:
                    syms.append(m); break
        return syms
    eur = [m for m in ex.markets if m.endswith("/EUR")]
    if eur: return eur
    usdt = [m for m in ex.markets if m.endswith("/USDT")]
    return usdt[:200]

def coin_dir(symbol:str) -> str:
    base = symbol.split("/")[0]
    d = os.path.join(DATA_DIR, EXCHANGE, base)
    os.makedirs(d, exist_ok=True)
    return d

def parquet_path(symbol:str) -> str:
    return os.path.join(coin_dir(symbol), f"{symbol.replace('/','_')}_1m.parquet")

def read_last_ts(parq:str):
    if not os.path.exists(parq): return None
    try:
        df = pd.read_parquet(parq, columns=["ts"])
        if df.empty: return None
        return int(df["ts"].max())
    except Exception:
        return None

async def fetch_symbol(symbol:str, ex, since_ms:int):
    parq = parquet_path(symbol)
    last_ts = read_last_ts(parq)
    if last_ts is not None:
        since_ms = last_ts + 60_000

    tf = "1m"
    limit = 1000

    def fetch_once(since):
        for tries in range(MAX_RETRIES):
            try:
                return ex.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=limit)
            except ccxt.RateLimitExceeded as re:
                time.sleep(1.5 * (tries+1))
            except Exception as e:
                time.sleep(0.5 * (tries+1))
        return []

    cursor = since_ms
    end_ms = now_utc_ms()
    rows = []
    while cursor < end_ms:
        batch = await asyncio.to_thread(fetch_once, cursor)
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + 60_000
        if len(batch) < limit:
            break
        await asyncio.sleep(0.05)

    if not rows and last_ts is not None:
        print(f"[{symbol}] up-to-date.")
        return True

    if rows:
        df_new = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
        df_new.drop_duplicates(subset=["ts"], inplace=True)
        df_new.sort_values("ts", inplace=True)

        if os.path.exists(parq):
            try:
                df_old = pd.read_parquet(parq)
                df = pd.concat([df_old, df_new], ignore_index=True)
                df.drop_duplicates(subset=["ts"], inplace=True)
                df.sort_values("ts", inplace=True)
            except Exception:
                df = df_new
        else:
            df = df_new

        # fill small gaps by forward fill
        df["ts_dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts_dt").asfreq("1min")
        df["ts"] = (df.index.view("int64") // 1_000_000).astype("int64")
        for col in ["open","high","low","close","volume"]:
            if col in df:
                df[col] = df[col].ffill()
        df.reset_index(drop=False, inplace=True)
        df.rename(columns={"ts_dt":"datetime"}, inplace=True)

        df.to_parquet(parq, index=False)
        print(f"[{symbol}] saved {len(df_new)} new rows → {parq}")
        return True

    print(f"[{symbol}] nothing fetched.")
    return True

async def memory_builder():
    print("=== Memory Builder: start ===")
    ex = mk_exchange()
    symbols = list_markets(ex)
    if not symbols:
        raise RuntimeError("No markets found.")
    start_dt = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    since_ms = utc_ms(start_dt)
    print(f"Fetching {len(symbols)} symbols since {start_dt.isoformat()}")

    sem = asyncio.Semaphore(MAX_CONC)
    async def worker(sym):
        async with sem:
            try:
                return await fetch_symbol(sym, ex, since_ms)
            except Exception as e:
                print("fetch err", sym, e)
                traceback.print_exc()
                return False

    tasks = [asyncio.create_task(worker(s)) for s in symbols]
    results = await asyncio.gather(*tasks)
    # Build simple DuckDB view
    os.makedirs(os.path.dirname(DUCKDB_PATH), exist_ok=True)
    try:
        con = duckdb.connect(DUCKDB_PATH)
        parq_glob = os.path.join(DATA_DIR, EXCHANGE, "*", "*_1m.parquet")
        con.execute("CREATE OR REPLACE VIEW v_ohlcv AS SELECT * FROM read_parquet($parq)",
                    parameters={"parq": parq_glob})
        con.close()
    except Exception as e:
        print("DuckDB index error:", e)

    tg_msg("✅ تم جمع المعلومات — يمكنك بدء تشغيل الفلترة")
    print("=== Memory Builder: done ===")

# ---- Filter Brain (sync) ----
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def scan_symbols_from_parquets() -> List[str]:
    base = os.path.join(DATA_DIR, EXCHANGE, "*", "*_1m.parquet")
    files = glob.glob(base)
    syms = [os.path.basename(p).replace("_1m.parquet","").replace("_","/") for p in files]
    return syms

def max_gain_from_trough(close_arr: np.ndarray) -> float:
    running_min = np.minimum.accumulate(close_arr)
    gains = (close_arr - running_min) / np.where(running_min==0, np.nan, running_min)
    return float(np.nanmax(gains) * 100.0)

def plot_series(symbol:str, df: pd.DataFrame, outdir:str):
    os.makedirs(outdir, exist_ok=True)
    fig = plt.figure(figsize=(10,3))
    plt.title(symbol)
    plt.plot(pd.to_datetime(df["ts"], unit="ms", utc=True), df["close"])
    plt.xlabel("time"); plt.ylabel("price")
    plt.tight_layout()
    p = os.path.join(outdir, f"{symbol.replace('/','_')}.png")
    plt.savefig(p, dpi=120)
    plt.close(fig)
    return p

def filter_brain():
    print("=== Filter Brain: start ===")
    con = None
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
    except Exception as e:
        print("DuckDB open error:", e)
        return

    syms = scan_symbols_from_parquets()
    kept = []
    dropped = []
    rows = []
    for sym in syms:
        pattern = sym.replace("/","_")
        try:
            df = con.execute(
                "SELECT ts, close FROM v_ohlcv WHERE regexp_matches(file, ?) ORDER BY ts",
                [pattern]
            ).df()
        except Exception as e:
            print("DuckDB query err for", sym, e)
            dropped.append(sym)
            continue

        if df.empty:
            dropped.append(sym); continue

        first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        pct_full = (last - first) / first * 100.0
        if abs(pct_full) <= FLAT_PCT:
            dropped.append(sym); continue

        mg = max_gain_from_trough(df["close"].to_numpy())
        if mg >= PUMP_THR:
            kept.append(sym)
            rows.append({"symbol": sym, "pct_full": pct_full, "max_gain_from_trough": mg, "n_minutes": len(df)})
            plot_series(sym, df, os.path.join(DATA_DIR, "plots"))
        else:
            dropped.append(sym)

    summary = pd.DataFrame(rows).sort_values("max_gain_from_trough", ascending=False)
    keep_path = os.path.join(DATA_DIR, "kept_symbols.csv")
    drop_path = os.path.join(DATA_DIR, "dropped_symbols.csv")
    summary.to_csv(keep_path, index=False)
    pd.DataFrame({"symbol": dropped}).to_csv(drop_path, index=False)

    tg_msg(f"✅ تم الفرز واستخراج المخططات\nالمحفوظ: {len(kept)} عملات\nالمستبعد: {len(dropped)}\nKept CSV: {keep_path}")
    print("=== Filter Brain: done ===")

# ---- Live Watcher (async) ----
import websockets

def binance_top_symbols(n:int=100):
    ex = ccxt.binance({'enableRateLimit': True})
    tickers = ex.fetch_tickers()
    rows = []
    for sym, tk in tickers.items():
        if not sym.endswith("/USDT"): continue
        vol_quote = tk.get("quoteVolume") or 0
        rows.append((sym, vol_quote))
    rows.sort(key=lambda x: x[1], reverse=True)
    syms = [s for s,_ in rows[:n]]
    return [s.replace("/USDT","").lower()+"usdt" for s in syms]

class KlineCache:
    def __init__(self, maxlen=180):
        self.maxlen = maxlen
        self.data = {}

    def push(self, sym, item):
        dq = self.data.setdefault(sym, deque(maxlen=self.maxlen))
        dq.append(item)

    def d1m_pct(self, sym):
        dq = self.data.get(sym)
        if not dq or len(dq) < 2: return 0.0
        a, b = dq[-2]["close"], dq[-1]["close"]
        if a == 0: return 0.0
        return (b - a) / a * 100.0

    def rvol5(self, sym):
        dq = self.data.get(sym)
        if not dq or len(dq) < 6: return 1.0
        vols = [x["vol"] for x in list(dq)[-6:-1]]
        baseline = sum(vols)/len(vols)
        cur = dq[-1]["vol"]
        if baseline == 0: return 1.0
        return cur / baseline

async def saqar_buy(coin:str, mode:str="maker", tp_eur:float=0.05, sl_pct:float=-2.0):
    if not SAQER_HOOK:
        print(f"[Saqer] (dry) {coin} mode={mode} tp={tp_eur} sl={sl_pct}")
        return
    import aiohttp
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"action":"buy","coin": coin, "tp_eur": tp_eur, "sl_pct": sl_pct, "mode": mode}
            async with session.post(SAQER_HOOK, json=payload, timeout=8) as r:
                txt = await r.text()
                print("Saqer resp:", r.status, txt[:200])
        except Exception as e:
            print("Saqer hook error:", e)

async def live_watcher():
    print("=== Live Watcher: start ===")
    symbols = binance_top_symbols(TOPN_BINANCE)
    streams = "/".join([f"{s}@kline_1m" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    kc = KlineCache(maxlen=180)

    tg_msg(f"👀 بدأ المراقبة الحية لعدد {len(symbols)} من عملات Binance (Top{TOPN_BINANCE})")
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                backoff = 1
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    k = data.get("data", {}).get("k", {})
                    if not k: continue
                    sym = k.get("s","").lower()
                    sym_std = sym.replace("usdt","/USDT").upper()
                    close = float(k["c"])
                    vol = float(k["q"])
                    ts = int(k["T"])
                    is_closed = bool(k["x"])

                    kc.push(sym, {"ts": ts, "close": close, "vol": vol, "is_closed": is_closed})

                    if is_closed:
                        d1 = kc.d1m_pct(sym)
                        rv = kc.rvol5(sym)
                        if d1 >= 0.35 and rv >= 1.5:
                            mode = "market" if (AGGRESSIVE and d1 >= 0.6) else "maker"
                            coin = sym_std.split("/")[0]
                            tg_msg(f"🚀 إشارة أولية: {sym_std} d1m={d1:.2f}% RVOL5≈{rv:.2f} mode={mode}")
                            await saqar_buy(coin, mode=mode, tp_eur=0.05, sl_pct=-2.0)
        except Exception as e:
            print("WS error:", e)
            traceback.print_exc()
            tg_msg("⚠️ Live watcher disconnected, reconnecting...")
            await asyncio.sleep(min(backoff, 60))
            backoff *= 2

# ---- Orchestration ----
def run_all_sync():
    try:
        asyncio.run(memory_builder())
    except Exception as e:
        print("Memory builder failed:", e)
        tg_msg(f"❌ Memory builder failed: {e}")
        return

    try:
        filter_brain()
    except Exception as e:
        print("Filter brain failed:", e)
        tg_msg(f"❌ Filter brain failed: {e}")
        return

    try:
        asyncio.run(live_watcher())
    except KeyboardInterrupt:
        print("Interrupted by user")
    except Exception as e:
        print("Live watcher crashed:", e)
        tg_msg(f"❌ Live watcher crashed: {e}")

if __name__ == "__main__":
    print("=== express_main.py starting ===")
    run_all_sync()