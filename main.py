# -*- coding: utf-8 -*-
"""
Express Pro v7 — Hotlist Surfer (Flask + Async WS)
- تجسّس Trades جماعي لكل الأسواق لبناء Hotlist ديناميكي
- مسّاح Surfer يعتمد OrderBook+Tape لكشف جدار حقيقي "يُؤكل" (لا يُسحب)
- إرسال فوري لإشارة الشراء إلى صقر بدون Top-1 لحظي
- /scan /ready /hotlist /health

ENV الأساسية:
  SAQAR_WEBHOOK="http://saqar:8080"
  LINK_SECRET="..."
  UNIVERSE="BTC-EUR,ETH-EUR,ADA-EUR,SOL-EUR,XRP-EUR"
  HOTLIST_SIZE=3
  HOTLIST_REFRESH_SEC=5
  HOTLIST_HYSTERESIS=0.2         # يقلّل تبدّل المراكز
  SPEED_WINDOW_SEC=60            # نافذة حساب السرعة
  AUTOSCAN_ON_START=1

تلغرام (اختياري):
  BOT_TOKEN, CHAT_ID

إستراتيجية الجدار (مثل v6، قابلة للضبط):
  DEPTH_LEVELS=30
  WALL_MIN_EUR=2500
  WALL_SIZE_RATIO=6.0
  HIT_RATIO_MIN=0.65
  DEPL_SPEED_MIN=0.15
  STICKY_SEC_MIN=3.0
  REPLENISH_OK_MAX=0.35
  SCORE_FOLLOW=0.75
  FRONT_TICKS=1
  TICK_SIZE_DEFAULT=0.0001
  MAX_HOLD_SIGNAL_SEC=20
  COOLDOWN_SEC=5

قيم صقر:
  TP_EUR=0.05
  SL_PCT=-2
"""

import os, json, time, math, threading, asyncio, statistics as st
from collections import deque, defaultdict
from typing import Dict, List, Tuple, Optional

import requests
import websockets
from flask import Flask, request, jsonify

# ====== إعدادات عامة ======
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "http://saqar:8080")
LINK_SECRET   = os.getenv("LINK_SECRET", "")

UNIVERSE = [m.strip() for m in os.getenv("UNIVERSE","BTC-EUR,ETH-EUR,ADA-EUR").split(",") if m.strip()]
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

# ====== أدوات ======
def tg_send(text:str):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text}, timeout=3)
    except Exception:
        pass

def px_round_down(px, tick): return math.floor(px/tick)*tick
def px_round_up(px, tick):   return math.ceil(px/tick)*tick

def market_tick_size(market:str)->float:
    return TICK_SIZE_DEFAULT

# ====== Hotlist: تجسّس Trades جماعي + ترتيب الكفاءة ======
class TradeSpy:
    """
    يشترك بقناة trades لكل أسواق UNIVERSE ويحسب:
      - vol_60s, trades_60s, avg_trade_size_60s, uptick_bias
    يحتفظ بنوافذ deque لكل سوق.
    """
    def __init__(self, markets:List[str]):
        self.markets = markets[:]
        self.windows: Dict[str, deque] = {m:deque(maxlen=2000) for m in self.markets}  # (ts, price, base)
        self.last_prices: Dict[str, float] = {m:0.0 for m in self.markets}
        self.uptick_counts: Dict[str, Tuple[int,int]] = {m:(0,0) for m in self.markets} # (ups,downs)
        self.loop=None
        self.thread=None
        self.stop=False

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self._runner, daemon=True)
        self.thread.start()

    def _runner(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run())
    
    async def _run(self):
        subs = [{"name":"trades","markets":self.markets}]
        async with websockets.connect(BITVAVO_WS, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"action":"subscribe","channels":subs}))
            while not self.stop:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("event")!="trade": 
                    continue
                for tr in msg.get("trades",[]):
                    market = tr.get("market")
                    if market not in self.windows: 
                        continue
                    price = float(tr["price"]); base=float(tr["amount"])
                    ts=time.time()
                    self.windows[market].append((ts, price, base))
                    lastp = self.last_prices.get(market,0.0)
                    if lastp>0:
                        ups,downs = self.uptick_counts.get(market,(0,0))
                        if price>lastp: ups+=1
                        elif price<lastp: downs+=1
                        self.uptick_counts[market]=(ups,downs)
                    self.last_prices[market]=price

    def metrics(self, market:str)->dict:
        now=time.time()
        w=self.windows.get(market)
        if not w or len(w)==0: 
            return {"vol_60s":0.0,"trades_60s":0,"avg_trade":0.0,"uptick":0.5,"score":0.0}
        win=[(ts,px,ba) for (ts,px,ba) in w if now-ts<=SPEED_WINDOW_SEC]
        vol=sum(ba for (_,_,ba) in win)
        cnt=len(win)
        avg=(vol/cnt) if cnt>0 else 0.0
        ups,downs=self.uptick_counts.get(market,(0,0))
        tot=ups+downs
        uptick = (ups/tot) if tot>0 else 0.5
        # Score كفاءة بسيط: مزيج سرعة + انحياز
        # طبّع vol و cnt داخلياً عبر log1p
        score = 0.6*(math.log1p(vol)) + 0.3*(math.log1p(cnt)) + 0.1*(max(0.0, (uptick-0.5))*2)
        return {"vol_60s":vol,"trades_60s":cnt,"avg_trade":avg,"uptick":round(uptick,3),"score":round(score,4)}

class HotlistManager:
    """
    يحدّث Hotlist كل HOTLIST_REFRESH_SEC اعتماداً على TradeSpy.score مع هستيريس.
    """
    def __init__(self, spy:TradeSpy, size:int):
        self.spy=spy
        self.size=size
        self.hotlist: List[str]=[]
        self.last_scores: Dict[str,float]=defaultdict(float)
        self.thread=None
        self.stop=False

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread=threading.Thread(target=self._runner, daemon=True)
        self.thread.start()

    def _runner(self):
        while not self.stop:
            self.refresh()
            time.sleep(HOTLIST_REFRESH_SEC)

    def refresh(self):
        # احسب السكور الحالي لكل سوق
        scores=[]
        for m in self.spy.markets:
            met=self.spy.metrics(m)
            s=met["score"]
            # هستيريس: إذا السوق موجود مسبقاً بالhotlist نرفع نقاطه قليلاً
            if m in self.hotlist: s += HOTLIST_HYSTERESIS
            self.last_scores[m]=s
            scores.append((s,m,met))
        scores.sort(reverse=True, key=lambda x:x[0])
        new_hot=[m for (_,m,_) in scores[:self.size]]
        if new_hot!=self.hotlist:
            self.hotlist=new_hot
            if BOT_TOKEN and CHAT_ID:
                lines=[f"{m} → score={self.last_scores[m]:.3f}  (vol60={self.spy.metrics(m)['vol_60s']:.2f}, cnt={self.spy.metrics(m)['trades_60s']})"
                       for m in self.hotlist]
                tg_send("🔥 Hotlist محدث:\n" + "\n".join(lines))

    def get_hotlist(self)->List[str]:
        return self.hotlist[:]

    def snapshot(self)->List[dict]:
        out=[]
        for m in self.hotlist:
            met=self.spy.metrics(m)
            out.append({"market":m, **met})
        return out

# ====== Surfer (سوق واحد) — كما v6 مع تحسينات طفيفة ======
class WallTrack:
    def __init__(self, side:str, price:float, size_base:float):
        self.side=side; self.price=price
        self.size0=size_base; self.size=size_base
        self.ts_first=time.time(); self.ts_last=self.ts_first
        self.hits_base=0.0; self.removed_base=0.0; self.replenished_base=0.0
        self.hit_events=0; self.cancel_events=0

    def update_size(self, new_size:float):
        now=time.time()
        delta=new_size-self.size
        if   delta>1e-12: self.replenished_base+=delta
        elif delta<-1e-12: self.removed_base+=(-delta); self.cancel_events+=1
        self.size=new_size; self.ts_last=now

    def record_trade_hit(self, base:float):
        self.hits_base+=base; self.hit_events+=1
        if self.removed_base>=base: self.removed_base-=base

    def life(self): return max(0.0, time.time()-self.ts_first)
    def depletion(self): return max(0.0, self.size0-self.size)

    def metrics(self):
        life=self.life(); depl=self.depletion()
        if self.size0<=1e-12 or life<=0:
            return dict(life=life,hit_ratio=0.0,depl_speed=0.0,replenish_ratio=0.0,progress=0.0)
        hit_ratio = self.hits_base/max(1e-12,depl)
        depl_speed= (depl/self.size0)/life
        repl_ratio= self.replenished_base/max(1e-12,self.size0)
        progress  = depl/self.size0
        return dict(life=life,hit_ratio=hit_ratio,depl_speed=depl_speed,replenish_ratio=repl_ratio,progress=progress)

    def score(self, avg_depth_eur:float)->Tuple[float,dict]:
        m=self.metrics()
        size_eur=self.size*self.price
        size_rel=min(2.0, (size_eur/max(1.0,avg_depth_eur)) if avg_depth_eur>0 else 2.0)
        sticky=min(1.0, m["life"]/max(1e-6,STICKY_SEC_MIN))
        hitq=max(0.0,min(1.0,(m["hit_ratio"]-HIT_RATIO_MIN+1.0)))/2.0
        speedq=max(0.0,min(1.0,(m["depl_speed"]/max(1e-6,DEPL_SPEED_MIN))))
        replq=1.0-min(1.0,m["replenish_ratio"]/max(1e-6,REPLENISH_OK_MAX))
        progq=m["progress"]
        S=0.20*size_rel+0.15*sticky+0.25*hitq+0.20*speedq+0.10*replq+0.10*progq
        return max(0.0,min(1.0,S)), dict(size_eur=size_eur,size_rel=size_rel,**m)

class MarketSurfer:
    def __init__(self, market:str):
        self.market=market
        self.tick=market_tick_size(market)
        self.bids:Dict[float,float]={}
        self.asks:Dict[float,float]={}
        self.best_bid=0.0; self.best_ask=0.0
        self.current_buy_wall:Optional[WallTrack]=None
        self.current_sell_wall:Optional[WallTrack]=None
        self.tape=deque(maxlen=600)  # (ts, side, price, base)
        self.last_signal_ts=0.0

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
        price=float(tr["price"]); base=float(tr["amount"]); side=tr.get("side","")
        ts=time.time()
        self.tape.append((ts,side,price,base))
        if self.current_buy_wall and abs(price-self.current_buy_wall.price)<1e-12 and side=="sell":
            self.current_buy_wall.record_trade_hit(base)
        if self.current_sell_wall and abs(price-self.current_sell_wall.price)<1e-12 and side=="buy":
            self.current_sell_wall.record_trade_hit(base)

    def _avg_depth_eur(self, side_dict:Dict[float,float])->float:
        if not side_dict: return 0.0
        items = sorted(side_dict.items(), key=(lambda x:-x[0] if side_dict is self.bids else lambda x:x[0]))
        # ^ ملاحظة: بايثون ما يقبل لامبدا مشروحة — نقسمها:
        if side_dict is self.bids:
            items = sorted(side_dict.items(), key=lambda x:-x[0])
        else:
            items = sorted(side_dict.items(), key=lambda x:x[0])
        total=0.0; n=0
        for i,(p,sz) in enumerate(items):
            if i>=DEPTH_LEVELS: break
            total+=p*sz; n+=1
        return (total/n) if n>0 else 0.0

    def _detect_wall(self, side:str)->Optional[Tuple[WallTrack,float]]:
        side_dict=self.bids if side=="buy" else self.asks
        if not side_dict: return None
        avg_eur=self._avg_depth_eur(side_dict)
        levels = (sorted(side_dict.items(), key=lambda x:-x[0]) if side=="buy"
                  else sorted(side_dict.items(), key=lambda x:x[0]))
        levels=levels[:DEPTH_LEVELS]
        for price,size in levels:
            size_eur=price*size
            if size_eur < WALL_MIN_EUR: continue
            if avg_eur>0 and (size_eur/avg_eur) < WALL_SIZE_RATIO: continue
            track=self.current_buy_wall if side=="buy" else self.current_sell_wall
            if track and abs(track.price-price)<1e-12:
                track.update_size(size); return track, avg_eur
            else:
                tr=WallTrack(side,price,size)
                if side=="buy": self.current_buy_wall=tr
                else: self.current_sell_wall=tr
                return tr, avg_eur
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
            snapshot_ok=False
            start=time.time()
            while True:
                # مهلة خارجية
                if time.time()-start>timeout_sec: return None
                raw=await ws.recv()
                msg=json.loads(raw)
                if msg.get("event")=="book":
                    self._apply_book(msg.get("bids",[]), msg.get("asks",[]))
                    snapshot_ok=True
                    self.current_buy_wall=None; self.current_sell_wall=None
                    continue
                if not snapshot_ok: 
                    continue
                if msg.get("event")=="bookUpdate":
                    self._apply_book(msg.get("bids",[]), msg.get("asks",[]))
                if msg.get("event")=="trade":
                    for tr in msg.get("trades",[]): self._apply_trade(tr)

                # تحديث جدار/كشف
                self._detect_wall("buy")
                # سيغنال؟
                now=time.time()
                if now-self.last_signal_ts < COOLDOWN_SEC: 
                    continue
                if self.current_buy_wall:
                    S,info = self.current_buy_wall.score(self._avg_depth_eur(self.bids))
                    conds = [
                        info["life"]>=STICKY_SEC_MIN,
                        info["hit_ratio"]>=HIT_RATIO_MIN,
                        info["depl_speed"]>=DEPL_SPEED_MIN,
                        info["replenish_ratio"]<=REPLENISH_OK_MAX,
                        S>=SCORE_FOLLOW
                    ]
                    if self.current_buy_wall.size<=1e-8:
                        self.current_buy_wall=None
                    elif all(conds):
                        entry=self.maker_entry_buy_px(self.current_buy_wall)
                        self.last_signal_ts=now
                        return {"type":"follow_buy","market":self.market,"price":entry,
                                "score":round(S,3),"wall_px":self.current_buy_wall.price,
                                "info":{k:(round(v,4) if isinstance(v,float) else v) for k,v in info.items()}}

# ====== مدير النظام ======
class ExpressManager:
    """
    - TradeSpy + HotlistManager خلفياً
    - يدور Surfer على hotlist فقط
    - أول إشارة → إرسال فوري إلى صقر → حالة SIGNAL_SENT حتى /ready
    """
    def __init__(self, universe:List[str]):
        self.spy=TradeSpy(universe)
        self.hot=HotlistManager(self.spy, HOTLIST_SIZE)
        self.state="IDLE"  # IDLE | SCANNING | SIGNAL_SENT
        self.run_id=0
        self._lock=threading.Lock()
        self.loop=None
        self.thread=None
        self.stop=False

    def start(self):
        if self.thread and self.thread.is_alive(): return
        # ابدأ التجسس والهات لست
        self.spy.start(); self.hot.start()
        # أطلق خيط المسح
        self.thread=threading.Thread(target=self._runner, daemon=True)
        self.thread.start()

    def _runner(self):
        self.loop=asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        if AUTOSCAN_ON_START:
            self.loop.run_until_complete(self.scan_cycle())
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
        self.state="SCANNING"
        my_run=self.inc_run()
        tg_send(f"🟢 بدء مسح (run {my_run}) — UNIVERSE={len(self.spy.markets)}, HOTLIST={HOTLIST_SIZE}")
        try:
            while not self.stop and self.state=="SCANNING" and my_run==self.cur_run():
                hot=self.hot.get_hotlist()
                if not hot:
                    await asyncio.sleep(1); continue
                # مر على الهوت لست بسرعة وأعط كل سوق مهلة قصيرة لإصطياد إشارة
                for m in hot:
                    if self.state!="SCANNING" or my_run!=self.cur_run(): break
                    surfer=MarketSurfer(m)
                    try:
                        res=await asyncio.wait_for(surfer.run_until_signal(timeout_sec=45), timeout=50)
                    except asyncio.TimeoutError:
                        res=None
                    except Exception as e:
                        tg_send(f"⚠️ Surfer error {m}: {e}"); res=None
                    if res and res.get("type")=="follow_buy":
                        ok=self.send_to_saqer(res)
                        self.state="SIGNAL_SENT"
                        info=res.get("info",{})
                        tg_send(
                            f"🚀 إشارة شراء ({m})\n"
                            f"Maker≈ {res['price']} | Score={res['score']} | wall={res['wall_px']}\n"
                            f"life={info.get('life')} | hit={info.get('hit_ratio')} | speed={info.get('depl_speed')} | "
                            f"repl={info.get('replenish_ratio')} | prog={info.get('progress')}\n"
                            f"إرسال لصقر: {'OK' if ok else 'FAILED'} — ⏸️ بانتظار /ready."
                        )
                        return
        finally:
            if self.state=="SCANNING":
                self.state="IDLE"; tg_send("ℹ️ انتهت دورة مسح بدون إشارة (IDLE).")

    def send_to_saqer(self, sig:dict)->bool:
        coin=sig["market"].split("-")[0]
        data={"action":"buy","coin":coin,"tp_eur":TP_EUR,"sl_pct":SL_PCT}
        headers={"X-Link-Secret":LINK_SECRET} if LINK_SECRET else {}
        try:
            r=requests.post(SAQAR_WEBHOOK+"/hook", json=data, headers=headers, timeout=5)
            return 200<=r.status_code<300
        except Exception as e:
            tg_send(f"⛔ فشل إرسال لصقر: {e}"); return False

    # واجهات Flask
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
        if reason: tg_send(f"✅ Ready من صقر (reason={reason}) — استئناف.")
        return {"ok":True,"state":self.state,"run":self.cur_run()}

    def api_health(self):
        return {"ok":True,"state":self.state,"run":self.cur_run(),
                "universe":len(self.spy.markets),"hotlist":self.hot.get_hotlist()}

    def api_hotlist(self):
        return {"ok":True,"hotlist":self.hot.snapshot()}

# ====== Flask ======
app=Flask(__name__)
manager=ExpressManager(UNIVERSE)

@app.route("/scan", methods=["GET","POST"])
def http_scan(): return jsonify(manager.api_scan())

@app.route("/ready", methods=["GET","POST"])
def http_ready():
    reason = request.json.get("reason") if request.is_json else None
    return jsonify(manager.api_ready(reason))

@app.route("/hotlist", methods=["GET"])
def http_hot(): return jsonify(manager.api_hotlist())

@app.route("/health", methods=["GET"])
def http_health(): return jsonify(manager.api_health())

# ====== تشغيل ======
if __name__=="__main__":
    manager.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8081")), threaded=True)