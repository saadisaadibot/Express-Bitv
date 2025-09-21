# -*- coding: utf-8 -*-
"""
Express Pro v7 — Hotlist Surfer + Hints (Flask + Async WS)
- يبني Hotlist ديناميكي (تجسّس trades جماعي) ويركّز المراقبة عليها.
- يكتشف جدار شراء "حقيقي" (يُؤكل بتنفيذ) ويرسل شراء لصقر فوراً.
- يكتب Hint في Redis: entry_hint/score/flash + يراقب بعد الإشارة لخروج مِرآتي (exit_now=1).
- يوفر /hotlist و /hint و /health و /scan و /ready.
"""

import os, json, time, math, threading, asyncio
from collections import deque, defaultdict
from typing import Dict, List, Tuple, Optional

import requests
import websockets
from flask import Flask, request, jsonify

# ====== إعدادات عامة ======
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "http://saqar:8080")
LINK_SECRET   = os.getenv("LINK_SECRET", "")

UNIVERSE = [m.strip() for m in os.getenv("UNIVERSE","BTC-EUR,ETH-EUR,ADA-EUR,SOL-EUR,XRP-EUR").split(",") if m.strip()]
AUTOSCAN_ON_START = os.getenv("AUTOSCAN_ON_START","1")=="1"

HOTLIST_SIZE        = int(os.getenv("HOTLIST_SIZE","3"))
HOTLIST_REFRESH_SEC = int(os.getenv("HOTLIST_REFRESH_SEC","5"))
HOTLIST_HYSTERESIS  = float(os.getenv("HOTLIST_HYSTERESIS","0.2"))
SPEED_WINDOW_SEC    = int(os.getenv("SPEED_WINDOW_SEC","60"))

# تلغرام (اختياري)
BOT_TOKEN = os.getenv("BOT_TOKEN","")
CHAT_ID   = os.getenv("CHAT_ID","")

# إستراتيجية الجدران
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
MAX_HOLD_SIGNAL  = float(os.getenv("MAX_HOLD_SIGNAL_SEC","20"))
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

def rset(key: str, obj: dict, ttl: int = 90):
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
                      json={"chat_id":CHAT_ID,"text":text}, timeout=3)
    except Exception:
        pass

def px_round_down(px, tick): return math.floor(px/tick)*tick
def px_round_up(px, tick):   return math.ceil(px/tick)*tick
def market_tick_size(market:str)->float: return TICK_SIZE_DEFAULT

# ===== Hotlist: تجسّس Trades =====
class TradeSpy:
    def __init__(self, markets:List[str]):
        self.markets=markets[:]
        self.windows={m:deque(maxlen=2000) for m in markets}  # (ts, price, base)
        self.last_price={m:0.0 for m in markets}
        self.updn={m:(0,0) for m in markets}
        self.loop=None; self.thread=None

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread=threading.Thread(target=self._runner, daemon=True); self.thread.start()

    def _runner(self):
        self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run())

    async def _run(self):
        subs=[{"name":"trades","markets":self.markets}]
        async with websockets.connect(BITVAVO_WS, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"action":"subscribe","channels":subs}))
            while True:
                msg=json.loads(await ws.recv())
                if msg.get("event")!="trade": continue
                for tr in msg.get("trades",[]):
                    m = tr.get("market"); 
                    if m not in self.windows: continue
                    px=float(tr["price"]); ba=float(tr["amount"]); ts=time.time()
                    self.windows[m].append((ts,px,ba))
                    lp=self.last_price.get(m,0.0)
                    u,d=self.updn[m]
                    if lp>0:
                        if px>lp: u+=1
                        elif px<lp: d+=1
                        self.updn[m]=(u,d)
                    self.last_price[m]=px

    def metrics(self, market:str)->dict:
        now=time.time(); win=[(ts,px,ba) for (ts,px,ba) in self.windows.get(market,[]) if now-ts<=SPEED_WINDOW_SEC]
        vol=sum(ba for (_,_,ba) in win); cnt=len(win); avg=(vol/cnt) if cnt else 0.0
        u,d=self.updn.get(market,(0,0)); tot=u+d; upt=(u/tot) if tot>0 else 0.5
        score = 0.6*math.log1p(vol) + 0.3*math.log1p(cnt) + 0.1*max(0.0,(upt-0.5))*2
        return {"vol_60s":vol,"trades_60s":cnt,"avg_trade":avg,"uptick":round(upt,3),"score":round(score,4)}

class HotlistManager:
    def __init__(self, spy:TradeSpy, size:int):
        self.spy=spy; self.size=size; self.hotlist:List[str]=[]
        self.thread=None; self.stop=False; self.last_scores=defaultdict(float)

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread=threading.Thread(target=self._runner, daemon=True); self.thread.start()

    def _runner(self):
        while not self.stop:
            self.refresh(); time.sleep(HOTLIST_REFRESH_SEC)

    def refresh(self):
        rows=[]
        for m in self.spy.markets:
            met=self.spy.metrics(m); s=met["score"]
            if m in self.hotlist: s += HOTLIST_HYSTERESIS
            self.last_scores[m]=s; rows.append((s,m,met))
        rows.sort(key=lambda x:x[0], reverse=True)
        new=[m for (_,m,_) in rows[:self.size]]
        if new!=self.hotlist:
            self.hotlist=new
            tg_send("🔥 Hotlist:\n"+"\n".join(f"{m} score={self.last_scores[m]:.3f}" for m in new))

    def get(self)->List[str]: return self.hotlist[:]
    def snapshot(self)->List[dict]: return [{"market":m, **self.spy.metrics(m)} for m in self.hotlist]

# ===== Surfer (كشف الجدران) =====
class WallTrack:
    def __init__(self, side:str, price:float, size_base:float):
        self.side=side; self.price=price
        self.size0=size_base; self.size=size_base
        self.ts_first=time.time(); self.ts_last=self.ts_first
        self.hits=0.0; self.removed=0.0; self.repl=0.0

    def update_size(self, new:float):
        now=time.time(); d=new-self.size
        if   d>1e-12: self.repl+=d
        elif d<-1e-12: self.removed+=(-d)
        self.size=new; self.ts_last=now

    def hit(self, qty:float):
        self.hits+=qty
        if self.removed>=qty: self.removed-=qty

    def life(self): return max(0.0,time.time()-self.ts_first)
    def deplet(self): return max(0.0, self.size0-self.size)

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
        size_rel=min(2.0, (size_eur/max(1.0,avg_depth_eur)) if avg_depth_eur>0 else 2.0)
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
        for px,sz in bids_upd:
            p=float(px); s=float(sz)
            if s<=0: self.bids.pop(p,None)
            else: self.bids[p]=s
        for px,sz in asks_upd:
            p=float(px); s=float(sz)
            if s<=0: self.asks.pop(p,None)
            else: self.asks[p]=s
        self._update_best()

    def _apply_trade(self, tr):
        p=float(tr["price"]); a=float(tr["amount"]); side=tr.get("side","")
        if self.buy and abs(p-self.buy.price)<1e-12 and side=="sell": self.buy.hit(a)
        if self.sell and abs(p-self.sell.price)<1e-12 and side=="buy": self.sell.hit(a)
        self.tape.append((time.time(), side, p, a))

    def _avg_depth(self, side_dict:Dict[float,float])->float:
        if not side_dict: return 0.0
        items=sorted(side_dict.items(), key=(lambda x:-x[0] if side_dict is self.bids else lambda x:x[0]))
        if side_dict is self.bids: items=sorted(side_dict.items(), key=lambda x:-x[0])
        else: items=sorted(side_dict.items(), key=lambda x:x[0])
        total=0.0; n=0
        for i,(p,sz) in enumerate(items):
            if i>=DEPTH_LEVELS: break
            total+=p*sz; n+=1
        return (total/n) if n>0 else 0.0

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
                if msg.get("event")=="book":
                    self._apply_book(msg.get("bids",[]), msg.get("asks",[])); snapshot_ok=True; self.buy=None; self.sell=None; continue
                if not snapshot_ok: continue
                if msg.get("event")=="bookUpdate":
                    self._apply_book(msg.get("bids",[]), msg.get("asks",[]))
                if msg.get("event")=="trade":
                    for tr in msg.get("trades",[]): self._apply_trade(tr)

                self._detect_wall("buy"); self._detect_wall("sell")
                if time.time()-self.last_signal_ts < COOLDOWN_SEC: continue
                if self.buy:
                    S,info=self.buy.score(self._avg_depth(self.bids))
                    conds = [info["life"]>=STICKY_SEC_MIN, info["hit_ratio"]>=HIT_RATIO_MIN,
                             info["depl_speed"]>=DEPL_SPEED_MIN, info["replenish_ratio"]<=REPLENISH_OK_MAX, S>=SCORE_FOLLOW]
                    if self.buy.size<=1e-8: self.buy=None
                    elif all(conds):
                        entry=self.maker_entry_buy_px(self.buy)
                        self.last_signal_ts=time.time()
                        return {"type":"follow_buy","market":self.market,"price":entry,
                                "score":round(S,3),"wall_px":self.buy.price,"info":info}

# ===== مدير النظام =====
def write_signal_hint(sig: dict):
    """يكتب Hint في Redis ليستعمله صقر."""
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

async def post_signal_watch(market: str):
    """يراقب 90ث بعد الإشارة: إذا انسحب buy wall أو ظهر sell wall قوي → exit_now=1"""
    key=f"express:signal:{market}"
    start=time.time()
    surfer=MarketSurfer(market)
    try:
        async with websockets.connect(BITVAVO_WS, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"action":"subscribe","channels":[
                {"name":"book","markets":[market]},
                {"name":"trades","markets":[market]}
            ]}))
            snapshot_ok=False
            while time.time()-start <= 90:
                msg=json.loads(await ws.recv())
                if msg.get("event")=="book":
                    surfer._apply_book(msg.get("bids",[]), msg.get("asks",[])); snapshot_ok=True; continue
                if not snapshot_ok: continue
                if msg.get("event")=="bookUpdate":
                    surfer._apply_book(msg.get("bids",[]), msg.get("asks",[]))
                if msg.get("event")=="trade":
                    for tr in msg.get("trades",[]): surfer._apply_trade(tr)
                surfer._detect_wall("buy"); surfer._detect_wall("sell")

                exit_flag=False
                if surfer.buy and surfer.buy.size<=1e-8:
                    exit_flag=True
                if surfer.sell:
                    S_s,info_s=surfer.sell.score(surfer._avg_depth(surfer.asks))
                    if S_s>=0.80 and info_s["hit_ratio"]>=HIT_RATIO_MIN:
                        exit_flag=True
                if exit_flag:
                    hint=rget(key) or {}
                    hint["exit_now"]=1
                    rset(key, hint, ttl=90)
                    return
    except Exception:
        return

class ExpressManager:
    def __init__(self, universe:List[str]):
        self.spy=TradeSpy(universe); self.hot=HotlistManager(self.spy, HOTLIST_SIZE)
        self.state="IDLE"; self.run_id=0; self._lock=threading.Lock()
        self.loop=None; self.thread=None; self.stop=False

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.spy.start(); self.hot.start()
        self.thread=threading.Thread(target=self._runner, daemon=True); self.thread.start()

    def _runner(self):
        self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        if AUTOSCAN_ON_START: self.loop.run_until_complete(self.scan_cycle())
        else:
            self.state="IDLE"; tg_send("🟡 Express v7 جاهز — /scan للبدء.")
            while not self.stop: time.sleep(0.2)

    def inc_run(self):
        with self._lock:
            self.run_id+=1; return self.run_id
    def cur_run(self):
        with self._lock:
            return self.run_id

    async def scan_cycle(self):
        self.state="SCANNING"; my=self.inc_run()
        tg_send(f"🟢 بدء مسح (run {my}) — HOTLIST={HOTLIST_SIZE}")
        try:
            while not self.stop and self.state=="SCANNING" and my==self.cur_run():
                hot=self.hot.get()
                if not hot: await asyncio.sleep(1); continue
                for m in hot:
                    if self.state!="SCANNING" or my!=self.cur_run(): break
                    surfer=MarketSurfer(m)
                    try:
                        res=await asyncio.wait_for(surfer.run_until_signal(timeout_sec=45), timeout=50)
                    except asyncio.TimeoutError:
                        res=None
                    except Exception as e:
                        tg_send(f"⚠️ Surfer error {m}: {e}"); res=None
                    if res and res.get("type")=="follow_buy":
                        write_signal_hint(res)
                        ok=self.send_to_saqer(res)
                        self.state="SIGNAL_SENT"
                        # راقب الخروج المرآتي
                        try: asyncio.run_coroutine_threadsafe(post_signal_watch(res["market"]), self.loop)
                        except Exception: pass
                        info=res.get("info",{})
                        tg_send(
                            f"🚀 BUY {m} | Maker≈{res['price']} | Score={res['score']}\n"
                            f"life={info.get('life'):.2f} hit={info.get('hit_ratio'):.2f} "
                            f"spd={info.get('depl_speed'):.3f} repl={info.get('replenish_ratio'):.2f}"
                        )
                        return
        finally:
            if self.state=="SCANNING":
                self.state="IDLE"; tg_send("ℹ️ لا إشارات — IDLE.")

    def send_to_saqer(self, sig:dict)->bool:
        coin=sig["market"].split("-")[0]
        data={"action":"buy","coin":coin,"tp_eur":TP_EUR,"sl_pct":SL_PCT}
        headers={"X-Link-Secret":LINK_SECRET} if LINK_SECRET else {}
        try:
            r=requests.post(SAQAR_WEBHOOK+"/hook", json=data, headers=headers, timeout=5)
            return 200<=r.status_code<300
        except Exception as e:
            tg_send(f"⛔ فشل إرسال لصقر: {e}"); return False

    def api_scan(self):
        self.state="SCANNING"
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.scan_cycle(), self.loop)
        else:
            self.start()
        return {"ok":True,"state":self.state,"run":self.cur_run()}

    def api_ready(self, reason:str=None):
        if self.state=="SIGNAL_SENT":
            self.state="SCANNING"
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self.scan_cycle(), self.loop)
            else:
                self.start()
        else:
            if self.state!="SCANNING":
                self.state="SCANNING"
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.scan_cycle(), self.loop)
                else:
                    self.start()
        if reason: tg_send(f"✅ Ready من صقر ({reason}) — استئناف.")
        return {"ok":True,"state":self.state,"run":self.cur_run()}

    def api_health(self): return {"ok":True,"state":self.state,"run":self.cur_run(),"hotlist":self.hot.get()}
    def api_hotlist(self): return {"ok":True,"hotlist":self.hot.snapshot()}

# ===== Flask =====
from flask import Flask, request, jsonify
app = Flask(__name__)
manager = ExpressManager(UNIVERSE)

def _auth_chat(chat_id: str) -> bool:
    return (not CHAT_ID) or (str(chat_id) == str(CHAT_ID))

def _tg_handle_cmd(text: str):
    t = (text or "").strip().lower()
    if t in ("/scan", "scan", "ابدأ", "سكان"):
        manager.api_scan(); tg_send("🔎 بدء المسح…"); return
    if t in ("/hotlist", "hotlist", "هوت", "قائمة"):
        snap = manager.api_hotlist().get("hotlist", [])
        if not snap: tg_send("ℹ️ لا توجد Hotlist بعد.")
        else:
            lines = [f"{r['market']}: score≈{r.get('score','?')} vol60={r.get('vol_60s','?')}" for r in snap]
            tg_send("🔥 Hotlist:\n" + "\n".join(lines))
        return
    if t in ("/health", "health", "حالة"):
        h = manager.api_health()
        tg_send(f"✅ state={h.get('state')} run={h.get('run')} hot={h.get('hotlist')}")
        return
    if t.startswith("/ready") or t == "ready":
        manager.api_ready("tg"); tg_send("🟢 Ready → استئناف المسح"); return
    tg_send("الأوامر: /scan ، /hotlist ، /health ، /ready")

@app.route("/scan", methods=["GET","POST"])
def http_scan(): return jsonify(manager.api_scan())

@app.route("/ready", methods=["GET","POST"])
def http_ready():
    reason = (request.get_json(silent=True) or {}).get("reason") if request.is_json else None
    return jsonify(manager.api_ready(reason))

@app.route("/hotlist", methods=["GET"])
def http_hot(): return jsonify(manager.api_hotlist())

@app.route("/health", methods=["GET"])
def http_health(): return jsonify(manager.api_health())

# ——— Telegram Webhook ———
# شغّال على /tg (الافتراضي عندك) وبنفس الوقت /webhook احتياط
@app.route("/tg", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def http_tg_webhook():
    upd = request.get_json(silent=True) or {}
    msg  = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not _auth_chat(chat_id) or not text:
        return jsonify(ok=True)
    try:
        _tg_handle_cmd(text)
    except Exception as e:
        tg_send(f"🐞 TG err: {type(e).__name__}: {e}")
    return jsonify(ok=True)

@app.route("/", methods=["GET"])
def home(): return "Express v7 ✅", 200

if __name__ == "__main__":
    manager.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8081")), threaded=True)