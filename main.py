# -*- coding: utf-8 -*-
"""
Express Pro v7 — Global Radar (ALL EUR) + Surfer Confirm + Hints
- اكتشاف تلقائي لكل أسواق EUR (UNIVERSE=AUTO).
- BurstRadar: رادار trades سريع لكل السوق عبر دفعات WebSocket.
- Surfer: تأكيد وجود buy wall حقيقي من دفتر الأوامر.
- إرسال لصقر + كتابة Hint في Redis + Webhook تيليغرام.
"""

import os, json, time, math, threading, asyncio
from collections import deque, defaultdict
from typing import Dict, List, Tuple, Optional

import requests
import websockets
from flask import Flask, request, jsonify

# ===== إعدادات عامة =====
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "http://saqar:8080")
LINK_SECRET   = os.getenv("LINK_SECRET", "")

UNIVERSE_ENV  = os.getenv("UNIVERSE","AUTO").strip()
DISCOVER_QUOTE= os.getenv("DISCOVER_QUOTE","EUR")
DISCOVER_REFRESH_MIN = int(os.getenv("DISCOVER_REFRESH_MIN","10"))

# رادار شامل
BURST_CHUNK            = int(os.getenv("BURST_CHUNK","40"))
BURST_SOCKETS          = int(os.getenv("BURST_SOCKETS","10"))
BURST_WINDOW_SEC       = int(os.getenv("BURST_WINDOW_SEC","10"))
BURST_MIN_TRADES_10S   = int(os.getenv("BURST_MIN_TRADES_10S","15"))
BURST_MIN_BASE_10S     = float(os.getenv("BURST_MIN_BASE_10S","1200"))
BURST_MIN_UPTICK       = float(os.getenv("BURST_MIN_UPTICK","0.60"))
BURST_COOLDOWN_SEC     = float(os.getenv("BURST_COOLDOWN_SEC","20"))

# تلغرام
BOT_TOKEN = os.getenv("BOT_TOKEN","")
CHAT_ID   = os.getenv("CHAT_ID","")

# Surfer (دفتر الأوامر)
DEPTH_LEVELS     = int(os.getenv("DEPTH_LEVELS","30"))
WALL_MIN_EUR     = float(os.getenv("WALL_MIN_EUR","2500"))
WALL_SIZE_RATIO  = float(os.getenv("WALL_SIZE_RATIO","6.0"))
HIT_RATIO_MIN    = float(os.getenv("HIT_RATIO_MIN","0.65"))
DEPL_SPEED_MIN   = float(os.getenv("DEPL_SPEED_MIN","0.15"))
STICKY_SEC_MIN   = float(os.getenv("STICKY_SEC_MIN","3.0"))
REPLENISH_OK_MAX = float(os.getenv("REPLENISH_OK_MAX","0.35"))
SCORE_FOLLOW     = float(os.getenv("SCORE_FOLLOW","0.75"))
FRONT_TICKS      = int(os.getenv("FRONT_TICKS","1"))
TICK_SIZE_DEFAULT= float(os.getenv("TICK_SIZE_DEFAULT","0.0001"))
COOLDOWN_SEC     = float(os.getenv("COOLDOWN_SEC","5"))

TP_EUR = float(os.getenv("TP_EUR","0.05"))
SL_PCT = float(os.getenv("SL_PCT","-2"))
BITVAVO_WS = "wss://ws.bitvavo.com/v2/"

# ===== Redis (اختياري) =====
REDIS_URL = os.getenv("REDIS_URL","")
R = None
try:
    import redis
    if REDIS_URL:
        R = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
except Exception:
    R = None

def rset(key: str, obj: dict, ttl: int = 120):
    if not R: return
    try: R.set(key, json.dumps(obj, separators=(',',':')), ex=ttl)
    except Exception: pass

def rget(key: str) -> Optional[dict]:
    if not R: return None
    try:
        s = R.get(key)
        return json.loads(s) if s else None
    except Exception:
        return None

# ===== أدوات =====
def tg_send(text:str):
    if not BOT_TOKEN or not CHAT_ID:
        print("TG:", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text}, timeout=5)
    except Exception:
        pass

def px_round_up(px, tick): return math.ceil(px/tick)*tick
def market_tick_size(_): return TICK_SIZE_DEFAULT

# ===== اكتشاف تلقائي لأسواق EUR =====
def auto_discover_markets(quote="EUR") -> List[str]:
    try:
        rows = requests.get("https://api.bitvavo.com/v2/markets", timeout=8).json()
        out = []
        for r in rows or []:
            if r.get("quote") == quote and r.get("status") == "trading":
                m = r.get("market")
                mq = float(r.get("minOrderInQuoteAsset",0) or 0)
                if m and mq <= 20:
                    out.append(m)
        return sorted(out)
    except Exception:
        return []

# ===== Surfer (كشف الجدران) =====
class WallTrack:
    def __init__(self, side:str, price:float, size_base:float):
        self.side=side; self.price=price
        self.size0=size_base; self.size=size_base
        self.ts_first=time.time(); self.ts_last=self.ts_first
        self.hits=0.0; self.removed=0.0; self.repl=0.0
    def update_size(self, new:float):
        d=new-self.size
        if d>1e-12: self.repl+=d
        elif d<-1e-12: self.removed+=(-d)
        self.size=new; self.ts_last=time.time()
    def hit(self, qty:float):
        self.hits+=qty
        if self.removed>=qty: self.removed-=qty
    def life(self): return max(0.0,time.time()-self.ts_first)
    def deplet(self): return max(0.0,self.size0-self.size)
    def metrics(self):
        life=self.life(); dep=self.deplet()
        if self.size0<=1e-12 or life<=0:
            return dict(life=life,hit_ratio=0.0,depl_speed=0.0,repl_ratio=0.0,progress=0.0)
        return dict(
            life=life,
            hit_ratio=(self.hits/max(1e-12,dep)),
            depl_speed=(dep/self.size0)/life,
            repl_ratio=self.repl/max(1e-12,self.size0),
            progress=dep/self.size0
        )
    def score(self, avg_depth_eur:float)->Tuple[float,dict]:
        m=self.metrics()
        size_eur=self.size*self.price
        size_rel=min(2.0,(size_eur/max(1.0,avg_depth_eur)) if avg_depth_eur>0 else 2.0)
        sticky=min(1.0, m["life"]/max(1e-6,STICKY_SEC_MIN))
        hitq=max(0.0,min(1.0,(m["hit_ratio"]-HIT_RATIO_MIN+1.0)))/2.0
        speedq=max(0.0,min(1.0,(m["depl_speed"]/max(1e-6,DEPL_SPEED_MIN))))
        replq=1.0-min(1.0,m["repl_ratio"]/max(1e-6,REPLENISH_OK_MAX))
        progq=m["progress"]
        S=0.20*size_rel+0.15*sticky+0.25*hitq+0.20*speedq+0.10*replq+0.10*progq
        return max(0.0,min(1.0,S)), dict(size_eur=size_eur,size_rel=size_rel,**m)

class MarketSurfer:
    def __init__(self, market:str):
        self.market=market; self.tick=market_tick_size(market)
        self.bids:Dict[float,float]={}; self.asks:Dict[float,float]={}
        self.best_bid=0.0; self.best_ask=0.0
        self.buy:Optional[WallTrack]=None; self.sell:Optional[WallTrack]=None
        self.tape=deque(maxlen=600); self.last_signal_ts=0.0

    def _update_best(self):
        if self.bids: self.best_bid=max(self.bids.keys())
        if self.asks: self.best_ask=min(self.asks.keys())

    def _apply_book(self, bids_upd, asks_upd):
        # حمايات: قد تأتي عناصر غير رقمية
        for itm in (bids_upd or []):
            if not (isinstance(itm, (list, tuple)) and len(itm)>=2): continue
            px,sz = itm[0], itm[1]
            try:
                p=float(px); s=float(sz)
            except Exception:
                continue
            if s<=0: self.bids.pop(p,None)
            else: self.bids[p]=s
        for itm in (asks_upd or []):
            if not (isinstance(itm, (list, tuple)) and len(itm)>=2): continue
            px,sz = itm[0], itm[1]
            try:
                p=float(px); s=float(sz)
            except Exception:
                continue
            if s<=0: self.asks.pop(p,None)
            else: self.asks[p]=s
        self._update_best()

    def _apply_trade(self, tr):
        try:
            p=float(tr["price"]); a=float(tr["amount"])
        except Exception:
            return
        side=str(tr.get("side",""))
        if self.buy and abs(p-self.buy.price)<1e-12 and side=="sell": self.buy.hit(a)
        if self.sell and abs(p-self.sell.price)<1e-12 and side=="buy": self.sell.hit(a)
        self.tape.append((time.time(), side, p, a))

    def _avg_depth(self, d:Dict[float,float])->float:
        if not d: return 0.0
        items = sorted(d.items(), key=(lambda kv: -kv[0])) if d is self.bids else sorted(d.items(), key=lambda kv: kv[0])
        total=0.0; n=0
        for i,(p,sz) in enumerate(items):
            if i>=DEPTH_LEVELS: break
            total+=p*sz; n+=1
        return (total/n) if n else 0.0

    def _detect_wall(self, side:str)->Optional[Tuple[WallTrack,float]]:
        side_dict=self.bids if side=="buy" else self.asks
        if not side_dict: return None
        avg=self._avg_depth(side_dict)
        levels=(sorted(side_dict.items(), key=lambda x:-x[0]) if side=="buy" else sorted(side_dict.items(), key=lambda x:x[0]))[:DEPTH_LEVELS]
        for price,size in levels:
            eur=price*size
            if eur < WALL_MIN_EUR: continue
            if avg>0 and (eur/avg) < WALL_SIZE_RATIO: continue
            track=self.buy if side=="buy" else self.sell
            if track and abs(track.price-price)<1e-12:
                track.update_size(size); return track, avg
            else:
                tr=WallTrack(side,price,size)
                if side=="buy": self.buy=tr
                else: self.sell=tr
                return tr, avg
        return None

    def maker_entry_buy_px(self, wall:WallTrack)->float:
        tgt=px_round_up(wall.price + FRONT_TICKS*self.tick, self.tick)
        if self.best_ask: tgt=min(tgt, self.best_ask - self.tick)
        return max(self.tick, tgt)

    async def run_until_signal(self, timeout_sec:int=60)->Optional[dict]:
        async with websockets.connect(BITVAVO_WS, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"action":"subscribe","channels":[
                {"name":"book","markets":[self.market]},
                {"name":"trades","markets":[self.market]}
            ]}))
            snapshot_ok=False; start=time.time()
            while True:
                if time.time()-start > timeout_sec: return None
                msg=json.loads(await ws.recv())
                ev=msg.get("event")

                if ev=="book":
                    bids = msg.get("bids") or []
                    asks = msg.get("asks") or []
                    if isinstance(bids, list) and isinstance(asks, list):
                        self._apply_book(bids, asks)
                        snapshot_ok=True; self.buy=None; self.sell=None
                    continue

                if not snapshot_ok: continue

                if ev=="bookUpdate":
                    bids = msg.get("bids") or []
                    asks = msg.get("asks") or []
                    if isinstance(bids, list) and isinstance(asks, list):
                        self._apply_book(bids, asks)

                elif ev=="trade":
                    self._apply_trade(msg)

                elif ev=="trades":
                    trs = msg.get("trades", [])
                    if isinstance(trs, list):
                        for tr in trs:
                            if isinstance(tr, dict): self._apply_trade(tr)

                # كشف الجدران
                self._detect_wall("buy"); self._detect_wall("sell")
                if time.time()-self.last_signal_ts < COOLDOWN_SEC: continue

                if self.buy:
                    S,info=self.buy.score(self._avg_depth(self.bids))
                    conds=[info["life"]>=STICKY_SEC_MIN, info["hit_ratio"]>=HIT_RATIO_MIN,
                           info["depl_speed"]>=DEPL_SPEED_MIN, info["repl_ratio"]<=REPLENISH_OK_MAX, S>=SCORE_FOLLOW]
                    if self.buy.size<=1e-8: self.buy=None
                    elif all(conds):
                        entry=self.maker_entry_buy_px(self.buy)
                        self.last_signal_ts=time.time()
                        return {"type":"follow_buy","market":self.market,"price":entry,
                                "score":round(S,3),"wall_px":self.buy.price,"info":info}

# ===== Burst Radar (للجميع) =====
class BurstRadar:
    """يشترك بالـtrades لكل الأسواق على دفعات WS، ويرصد انفجار خلال 10s."""
    def __init__(self, markets: List[str], on_burst_cb):
        self.markets = markets[:]
        self.on_burst = on_burst_cb
        self.buffers  = {m: deque(maxlen=4000) for m in self.markets}  # (ts, px, base, up?)
        self.seen_recent = defaultdict(lambda: 0.0)
        self.threads = []; self.stop=False

    def start(self):
        if self.threads: return
        chunks = [self.markets[i:i+BURST_CHUNK] for i in range(0, len(self.markets), BURST_CHUNK)]
        chunks = chunks[:max(1, BURST_SOCKETS)]
        for i, ch in enumerate(chunks):
            t = threading.Thread(target=self._runner, args=(ch,i), daemon=True)
            t.start(); self.threads.append(t)
        tg_send(f"📡 Radar on {len(chunks)} WS sessions / {len(self.markets)} markets.")

    def _runner(self, markets_chunk: List[str], idx: int):
        async def _run():
            subs=[{"name":"trades","markets":markets_chunk}]
            try:
                async with websockets.connect(BITVAVO_WS, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"action":"subscribe","channels":subs}))
                    last_eval=time.time()
                    last_px = {m:0.0 for m in markets_chunk}
                    while not self.stop:
                        msg=json.loads(await ws.recv())
                        ev=msg.get("event")
                        if ev not in ("trade","trades"): continue
                        trs=[msg] if ev=="trade" else (msg.get("trades",[]) or [])
                        now=time.time()
                        if not isinstance(trs, list):  # حماية
                            continue
                        for tr in trs:
                            if not isinstance(tr, dict): 
                                continue
                            m=tr.get("market")
                            if m not in self.buffers: continue
                            try:
                                px=float(tr.get("price",0) or 0); ba=float(tr.get("amount",0) or 0)
                            except Exception:
                                continue
                            prev = last_px.get(m, px)
                            up = 1 if px >= prev else 0
                            last_px[m]=px
                            self.buffers[m].append((now, px, ba, up))
                        if now - last_eval >= 0.5:
                            self._maybe_burst(markets_chunk, now)
                            last_eval = now
            except Exception as e:
                tg_send(f"⚠️ radar[{idx}] ws err: {e}")
        asyncio.run(_run())

    def _maybe_burst(self, markets_chunk: List[str], now: float):
        for m in markets_chunk:
            buf=self.buffers.get(m); 
            if not buf: continue
            win=[x for x in list(buf) if now - x[0] <= BURST_WINDOW_SEC]
            cnt=len(win)
            if cnt==0: continue
            base_sum = sum(x[2] for x in win)
            uptick   = (sum(x[3] for x in win) / cnt) if cnt else 0.5
            if now - self.seen_recent[m] < BURST_COOLDOWN_SEC:
                continue
            if (cnt >= BURST_MIN_TRADES_10S and base_sum >= BURST_MIN_BASE_10S and uptick >= BURST_MIN_UPTICK):
                self.seen_recent[m]=now
                try: self.on_burst(m, {"cnt":cnt, "base":base_sum, "uptick":round(uptick,2)})
                except Exception as e: tg_send(f"on_burst err {m}: {e}")

# ===== Hint + إرسال لصقر =====
def write_signal_hint(sig: dict):
    key=f"express:signal:{sig['market']}"
    hint={
        "ts": int(time.time()),
        "entry_hint": float(sig.get("price") or 0.0),
        "score": float(sig.get("score") or 0.0),
        "wall_px": float(sig.get("wall_px") or 0.0),
        "flash": 1 if float(sig.get("score") or 0.0) >= 0.78 else 0,
        "exit_now": 0
    }
    rset(key, hint, ttl=120)

# ===== المدير =====
class ExpressManager:
    def __init__(self, universe_env: str):
        if universe_env.upper() == "AUTO" or not universe_env:
            self.markets = auto_discover_markets(DISCOVER_QUOTE)
            tg_send(f"🌐 AUTO اكتشف {len(self.markets)} سوق {DISCOVER_QUOTE}.")
        else:
            self.markets = [m.strip() for m in universe_env.split(",") if m.strip()]
        self.state="IDLE"; self.loop=None; self.thread=None; self.stop=False
        self.radar = BurstRadar(self.markets, on_burst_cb=self._on_burst)
        if DISCOVER_REFRESH_MIN > 0:
            threading.Thread(target=self._rediscover_loop, daemon=True).start()

    def _rediscover_loop(self):
        while True:
            time.sleep(max(120, DISCOVER_REFRESH_MIN*60))
            try:
                new = auto_discover_markets(DISCOVER_QUOTE)
                if new and set(new) != set(self.markets):
                    self.markets = new
                    self.radar.stop = True
                    self.radar = BurstRadar(self.markets, on_burst_cb=self._on_burst)
                    self.radar.start()
                    tg_send(f"🔄 أعيد الاكتشاف: {len(new)} سوق.")
            except Exception as e:
                tg_send(f"rediscover err: {e}")

    def _on_burst(self, market:str, meta:dict):
        def _task():
            try:
                tg_send(f"📡 Burst {market} cnt={meta['cnt']} base={meta['base']:.0f} upt={meta['uptick']}")
                res = asyncio.run(MarketSurfer(market).run_until_signal(timeout_sec=35))
                if res and res.get("type")=="follow_buy":
                    write_signal_hint(res)
                    _ = self.send_to_saqer(res)
                    info=res.get("info",{})
                    tg_send(
                        f"🚀 BUY {market} Maker≈{res['price']} S={res['score']}\n"
                        f"life={info.get('life'):.2f} hit={info.get('hit_ratio'):.2f} "
                        f"spd={info.get('depl_speed'):.3f} repl={info.get('repl_ratio'):.2f}"
                    )
            except Exception as e:
                tg_send(f"burst->surfer err {market}: {e}")
        threading.Thread(target=_task, daemon=True).start()

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread=threading.Thread(target=self._runner, daemon=True); self.thread.start()
        self.radar.start()

    def _runner(self):
        self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        self.state="IDLE"; tg_send("🟡 Express v7 Global Radar جاهز. /scan لتشغيل الرادار.")
        while not self.stop: time.sleep(1.0)

    def send_to_saqer(self, sig:dict)->bool:
        coin=sig["market"].split("-")[0]
        data={"action":"buy","coin":coin,"tp_eur":TP_EUR,"sl_pct":SL_PCT}
        headers={"X-Link-Secret":LINK_SECRET} if LINK_SECRET else {}
        try:
            r=requests.post(SAQAR_WEBHOOK+"/hook", json=data, headers=headers, timeout=6)
            return 200<=r.status_code<300
        except Exception as e:
            tg_send(f"⛔ فشل إرسال لصقر: {e}"); return False

    # واجهات HTTP بسيطة
    def api_scan(self): self.start(); return {"ok":True,"state":self.state,"markets":len(self.markets)}
    def api_ready(self, reason:str=None):
        self.start()
        if reason: tg_send(f"✅ Ready ({reason})")
        return {"ok":True,"state":self.state}
    def api_health(self): return {"ok":True,"state":self.state,"markets":len(self.markets)}

# ===== Flask =====
app = Flask(__name__)
manager = ExpressManager(UNIVERSE_ENV)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

def _tg_handle_cmd(text: str):
    t=(text or "").strip().lower()
    if t in ("/scan","scan","ابدأ","start"):
        res=manager.api_scan(); tg_send(f"🔎 تم تشغيل الرادار… ({res.get('markets')} markets)")
        return
    if t in ("/health","health","حالة"):
        h=manager.api_health(); tg_send(f"✅ state={h.get('state')} markets={h.get('markets')}")
        return
    if t.startswith("/ready") or t=="ready":
        manager.api_ready("tg"); tg_send("🟢 Ready")
        return
    tg_send("الأوامر: /scan ، /health ، /ready")

@app.route("/scan", methods=["GET","POST"])
def http_scan(): return jsonify(manager.api_scan())

@app.route("/ready", methods=["GET","POST"])
def http_ready():
    reason=(request.get_json(silent=True) or {}).get("reason") if request.is_json else None
    return jsonify(manager.api_ready(reason))

@app.route("/health", methods=["GET"])
def http_health(): return jsonify(manager.api_health())

@app.route("/hint", methods=["GET"])
def http_hint():
    m=request.args.get("market","")
    data=rget(f"express:signal:{m}") if m else None
    return jsonify({"ok": bool(data), "hint": data or {}})

@app.route("/tg", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def http_tg():
    upd=request.get_json(silent=True) or {}
    msg=upd.get("message") or upd.get("edited_message") or {}
    chat=msg.get("chat") or {}
    chat_id=str(chat.get("id") or "")
    text=(msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id) or not text:
        return jsonify(ok=True)
    try: _tg_handle_cmd(text)
    except Exception as e: tg_send(f"🐞 TG err: {type(e).__name__}: {e}")
    return jsonify(ok=True)

@app.route("/", methods=["GET"])
def home(): return "Express v7 Global Radar ✅", 200

if __name__ == "__main__":
    manager.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8081")), threaded=True)