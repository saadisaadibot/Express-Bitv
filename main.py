# -*- coding: utf-8 -*-
"""
Daily Accumulation & ExplodeSoon Scanner (Bitvavo EUR) — Research Mode
- يمسح كل أزواج -EUR ببطء لكن بدقة، يُخرِج Top10 + Daily Pick لعملة مرشحة للانفجار خلال 24h.
- لا يغيّر أي شيء في بوت التداول. فقط تحليل/تلغرام.

ENV:
  BOT_TOKEN, CHAT_ID (اختياريان لإرسال الملخص)
  REDIS_URL (اختياري للتخزين المؤقت)
"""

import os, time, json, math, statistics as st, traceback
from collections import deque, defaultdict
import requests, redis

BASE_URL = "https://api.bitvavo.com/v2"
HTTP_TIMEOUT = 8

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL) if REDIS_URL else None

# ---------- Utils ----------
def tg(msg):
    if not (BOT_TOKEN and CHAT_ID): print(msg); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg[:4000]},
            timeout=7
        )
    except Exception as e:
        print("TG err:", e)

def get(url, timeout=HTTP_TIMEOUT):
    return requests.get(url, timeout=timeout).json()

def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x

def pct(a,b):  # ((a/b)-1)*100
    return ((a/b)-1.0)*100.0 if b>0 else 0.0

def linreg_slope(y):
    """انحدار خطي بسيط (slope) على لوغ السعر — بدون numpy."""
    n = len(y)
    if n < 3: return 0.0
    xs = range(n)
    xbar = (n-1)/2.0
    ybar = sum(y)/n
    num = sum((x-xbar)*(y[x]-ybar) for x in xs)
    den = sum((x-xbar)*(x-xbar) for x in xs)
    return num/den if den>0 else 0.0

# ---------- Data pulls ----------
def list_markets_eur():
    rows = get(f"{BASE_URL}/ticker/24h")
    mkts = []
    if isinstance(rows, list):
        for r0 in rows:
            m = r0.get("market","")
            if m.endswith("-EUR"):
                try:
                    last = float(r0.get("last",0) or 0)
                    if last>0: mkts.append(m)
                except: pass
    return mkts

def candles(market, interval="1m", limit=300):
    try:
        rows = get(f"{BASE_URL}/{market}/candles?interval={interval}&limit={limit}")
        # row: [ts,open,high,low,close,volume]
        return rows if isinstance(rows, list) else []
    except Exception as e:
        return []

def orderbook(market, depth=3):
    try:
        j = get(f"{BASE_URL}/{market}/book")
        if not j or not j.get("bids") or not j.get("asks"): return None
        bids = [(float(p), float(q)) for p,q,*_ in j["bids"][:depth]]
        asks = [(float(p), float(q)) for p,q,*_ in j["asks"][:depth]]
        best_bid, best_ask = bids[0][0], asks[0][0]
        spread_bp = (best_ask-best_bid)/((best_ask+best_bid)/2.0) * 10000.0
        bid_eur = sum(p*q for p,q in bids)
        ask_eur = sum(p*q for p,q in asks)
        imb = (bid_eur / max(1e-9, ask_eur))
        return {"spread_bp": spread_bp, "imb": imb, "bid_eur": bid_eur, "ask_eur": ask_eur}
    except Exception:
        return None

# ---------- Feature engineering ----------
def compute_features(market):
    """
    يُرجع dict بميزات:
      slope1h, slope3h (٪/ساعة تقريبًا على لوغ السعر)
      accel, r15, squeeze_ratio, vol_push, ob_spread_bp, ob_imb, near_high
    """
    c1 = candles(market, "1m", 240)  # ~4h
    if len(c1) < 120: return None
    closes = [float(c[4]) for c in c1]
    vols   = [float(c[5]) for c in c1]
    # لوغ السعر يُحسّن الانحدار
    logs = [math.log(max(1e-12, p)) for p in closes]

    # نوافذ
    last60  = logs[-60:]    # ~1h
    last180 = logs[-180:]   # ~3h
    slope1h = linreg_slope(last60)   * 60  # لكل ساعة (تقريبي)
    slope3h = linreg_slope(last180)  * 60
    accel   = slope1h - slope3h

    # r15 (%)
    p_now = closes[-1]; p_15 = closes[-16] if len(closes)>=16 else closes[0]
    r15 = pct(p_now, p_15)

    # Squeeze (Bollinger bandwidth now / median of last 3h)
    def boll_width(arr, n=20):
        if len(arr)<n: return None
        sub = arr[-n:]
        m = sum(sub)/n
        std = math.sqrt(sum((x-m)**2 for x in sub)/n)
        up, dn = m+2*std, m-2*std
        return (up-dn)/max(1e-12, m)
    bw_now = boll_width(closes, 20) or 0.0
    bws = []
    for i in range(60, 180):  # لقطات قديمة داخل 3h
        w = boll_width(closes[:i], 20)
        if w: bws.append(w)
    median_bw = st.median(bws) if bws else bw_now
    squeeze_ratio = bw_now / max(1e-9, median_bw)  # أصغر = انقباض أقوى

    # Volume push (آخر 10m / متوسط 60m)
    v10 = sum(vols[-10:]) / max(1, 10)
    v60 = sum(vols[-60:]) / max(1, 60)
    vol_push = v10 / max(1e-9, v60)

    # قرب من قمة 24h (وزن خفيف)
    hi_24 = max(closes[-240:]) if len(closes)>=240 else max(closes)
    near_high = p_now / max(1e-9, hi_24)

    ob = orderbook(market) or {}
    ob_spread_bp = ob.get("spread_bp", 999.0)
    ob_imb = ob.get("imb", 0.0)

    return {
        "market": market,
        "slope1h": slope1h, "slope3h": slope3h, "accel": accel, "r15": r15,
        "squeeze_ratio": squeeze_ratio, "vol_push": vol_push,
        "ob_spread_bp": ob_spread_bp, "ob_imb": ob_imb, "near_high": near_high,
        "price": p_now,
    }

def score_row(feat):
    # تطبيع وخليط نقاط (0–100)
    accel = clamp(feat["accel"], -0.5, 0.5)
    r15   = clamp(feat["r15"],  -1.5,  1.5)
    spread= feat["ob_spread_bp"]
    imb   = feat["ob_imb"]
    squeeze = feat["squeeze_ratio"]
    volp    = clamp(feat["vol_push"], 0.5, 3.0)

    # Momentum/Accel (0–45)
    mom = 25.0 * clamp((accel - 0.02)/0.18, 0.0, 1.0) + 20.0 * clamp((r15 - 0.05)/0.30, 0.0, 1.0)

    # Squeeze أفضل لما <1 (انقباض)
    sq  = 20.0 * clamp((1.2 - squeeze)/1.0, 0.0, 1.0)

    # Volume push (0–15)
    vp  = 15.0 * clamp((volp - 0.9)/1.6, 0.0, 1.0)

    # Order-book (0–20): سبريد ضيق + ميل قوي
    ob_sp = 10.0 * clamp((200.0 - spread)/150.0, 0.0, 1.0)
    ob_im = 10.0 * clamp((imb - 0.95)/0.6,       0.0, 1.0)

    score = mom + sq + vp + ob_sp + ob_im
    return score

# ---------- Main scan ----------
def scan_all(topn=10):
    mkts = list_markets_eur()
    rows = []
    for i, m in enumerate(mkts):
        try:
            f = compute_features(m)
            if not f: continue
            s = score_row(f)
            f["score"] = round(s, 2)
            rows.append(f)
            if r:
                r.hset("daily:features", m, json.dumps(f))
        except Exception as e:
            print("scan err", m, e)
        # بطيء لكن ثابت
        time.sleep(0.12)
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:topn], (rows[0] if rows else None)

def main_once():
    top, best = scan_all(10)
    if not best:
        tg("❌ لا نتائج."); return
    lines = ["📈 Daily ExplodeSoon — Top 10 (Bitvavo EUR)\n"]
    for i, f in enumerate(top, 1):
        m = f["market"].replace("-EUR","")
        lines.append(f"{i:>2}. {m:<8} | score {f['score']:.1f} | acc {f['accel']:.3f} | r15 {f['r15']:+.2f}% | sq {f['squeeze_ratio']:.2f} | vol× {f['vol_push']:.2f} | ob {f['ob_spread_bp']:.0f}bp/{f['ob_imb']:.2f}")
    lines.append("\n⭐️ Daily Pick: " + best["market"].replace("-EUR","") + f" — score {best['score']:.1f} @ €{best['price']:.6f}")
    msg = "\n".join(lines)
    tg(msg)
    if r:
        r.set("daily:last_pick", best["market"])

if __name__ == "__main__":
    main_once()