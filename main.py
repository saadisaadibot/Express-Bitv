# -*- coding: utf-8 -*-
"""
Express Pro v6 — Liquidity Surfer Edition (Flask + Async WS)
- بديل "أبو صياح": سكان ذكي يعتمد OrderBook + Tape لكشف الجدران الحقيقية
- يولّد follow_buy لصقر عبر /hook عندما تتوافر الشروط (Wall Score ≥ العتبة)
- يدور على لائحة أسواق (MARKETS) بالتتابع، سوق واحد فعّال في كل لحظة
- يتوقف بعد إرسال إشارة وينتظر /ready من صقر ثم يستأنف المسح
- /scan: يبدأ جولة جديدة فوراً (يلغي القديمة)
- /ready: يضع الحالة "جاهز" ويستأنف المسح
- /health: فحص سريع

ENV (أمثلة أساسية):
  SAQAR_WEBHOOK="http://saqar:8080"    # بدون /hook
  LINK_SECRET="..."
  MARKETS="BTC-EUR,ETH-EUR,ADA-EUR"    # قائمة أسواق
  AUTOSCAN_ON_START=1                  # يبدأ مسح تلقائي عند التشغيل
  BOT_TOKEN="123:ABC"                  # اختياري
  CHAT_ID="-100123456"                 # اختياري

ENV (عناصر الإستراتيجية الافتراضية):
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
  TP_EUR=0.05               # هدف ربح أولي لإشارة صقر
  SL_PCT=-2                 # ستوب لصقر (%-)

ملاحظات:
- يعتمد Bitvavo WebSocket: book + trades
- متوافق تماماً مع صقر: يرسل {"action":"buy","coin":..., "tp_eur":..., "sl_pct":...}
- إذا أردت دمج فلاتر إضافية (EMA/VWAP) لاحقاً سهلة الإضافة قبل إطلاق الإشارة
"""

import os, json, time, math, threading, asyncio, statistics as st
from collections import deque
from typing import Dict, Tuple, Optional

import requests
import websockets
from flask import Flask, request, jsonify

# ========= إعدادات عامة =========
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "http://saqar:8080")
LINK_SECRET   = os.getenv("LINK_SECRET", "")

MARKETS = [m.strip() for m in os.getenv("MARKETS", "BTC-EUR,ETH-EUR,ADA-EUR").split(",") if m.strip()]
AUTOSCAN_ON_START = os.getenv("AUTOSCAN_ON_START", "1") == "1"

# تليغرام (اختياري)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")

# معلمات الإستراتيجية
DEPTH_LEVELS     = int(os.getenv("DEPTH_LEVELS", "30"))
WALL_MIN_EUR     = float(os.getenv("WALL_MIN_EUR", "2500"))
WALL_SIZE_RATIO  = float(os.getenv("WALL_SIZE_RATIO", "6.0"))
HIT_RATIO_MIN    = float(os.getenv("HIT_RATIO_MIN", "0.65"))
DEPL_SPEED_MIN   = float(os.getenv("DEPL_SPEED_MIN", "0.15"))
STICKY_SEC_MIN   = float(os.getenv("STICKY_SEC_MIN", "3.0"))
REPLENISH_OK_MAX = float(os.getenv("REPLENISH_OK_MAX", "0.35"))
SCORE_FOLLOW     = float(os.getenv("SCORE_FOLLOW", "0.75"))
FRONT_TICKS      = int(os.getenv("FRONT_TICKS", "1"))
TICK_SIZE_DEFAULT= float(os.getenv("TICK_SIZE_DEFAULT", "0.0001"))
MAX_HOLD_SIGNAL  = float(os.getenv("MAX_HOLD_SIGNAL_SEC", "20"))
COOLDOWN_SEC     = float(os.getenv("COOLDOWN_SEC", "5"))

TP_EUR           = float(os.getenv("TP_EUR", "0.05"))
SL_PCT           = float(os.getenv("SL_PCT", "-2"))

BITVAVO_WS       = "wss://ws.bitvavo.com/v2/"

# ========= أدوات عامة =========
def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=3)
    except Exception:
        pass

def px_round_down(px, tick):  return math.floor(px/tick)*tick
def px_round_up(px, tick):    return math.ceil(px/tick)*tick

def market_tick_size(market: str) -> float:
    # إن لم تكن تعرف دقة السوق من API، استخدم الافتراضي
    # (يمكن لاحقاً جلبها من REST: /markets )
    return TICK_SIZE_DEFAULT

# ========= تتبع الجدران =========
class WallTrack:
    def __init__(self, side: str, price: float, size_base: float):
        self.side = side               # "buy" أو "sell"
        self.price = price
        self.size0 = size_base
        self.size  = size_base
        self.ts_first = time.time()
        self.ts_last  = self.ts_first
        self.hits_base = 0.0           # تنفيذ فعلي عند السعر (من tape)
        self.removed_base = 0.0        # إزالة بدون تنفيذ (cancel)
        self.replenished_base = 0.0    # إعادة تعبئة
        self.hit_events = 0
        self.cancel_events = 0

    def update_size(self, new_size: float):
        now = time.time()
        delta = new_size - self.size
        if delta > 1e-12:
            self.replenished_base += delta
        elif delta < -1e-12:
            self.removed_base += (-delta)
            self.cancel_events += 1
        self.size = new_size
        self.ts_last = now

    def record_trade_hit(self, base_qty: float):
        self.hits_base += base_qty
        self.hit_events += 1
        if self.removed_base >= base_qty:
            self.removed_base -= base_qty

    def life_sec(self) -> float:
        return max(0.0, time.time() - self.ts_first)

    def depletion(self) -> float:
        return max(0.0, self.size0 - self.size)

    def metrics(self, size0_eps=1e-12):
        life = self.life_sec()
        depl = self.depletion()
        if self.size0 <= size0_eps or life <= 0:
            return dict(life=life, hit_ratio=0.0, depl_speed=0.0,
                        replenish_ratio=0.0, progress=0.0)
        hit_ratio = (self.hits_base / max(size0_eps, depl))
        depl_speed = (depl / self.size0) / life
        replenish_ratio = self.replenished_base / max(size0_eps, self.size0)
        progress = depl / self.size0
        return dict(
            life=life, hit_ratio=hit_ratio, depl_speed=depl_speed,
            replenish_ratio=replenish_ratio, progress=progress
        )

    def score(self, avg_depth_eur: float, price_to_eur_coef: float = 1.0) -> Tuple[float, Dict]:
        m = self.metrics()
        size_eur = self.size * self.price * price_to_eur_coef
        size_rel = min(2.0, (size_eur / max(1.0, avg_depth_eur)) if avg_depth_eur>0 else 2.0)

        sticky  = min(1.0, m["life"]/max(1e-6, STICKY_SEC_MIN))
        hitq    = max(0.0, min(1.0, (m["hit_ratio"] - HIT_RATIO_MIN + 1.0))) / 2.0
        speedq  = max(0.0, min(1.0, (m["depl_speed"]/max(DEPL_SPEED_MIN,1e-6))))
        replq   = 1.0 - min(1.0, m["replenish_ratio"]/max(REPLENISH_OK_MAX,1e-6))
        progq   = m["progress"]

        S = (0.20*size_rel + 0.15*sticky + 0.25*hitq + 0.20*speedq + 0.10*replq + 0.10*progq)
        return max(0.0, min(1.0, S)), dict(size_eur=size_eur, size_rel=size_rel, **m)

# ========= ماسح سوق واحد =========
class MarketSurfer:
    def __init__(self, market: str):
        self.market = market
        self.tick   = market_tick_size(market)
        self.bids: Dict[float,float] = {}
        self.asks: Dict[float,float] = {}
        self.best_bid = 0.0
        self.best_ask = 0.0

        self.current_buy_wall: Optional[WallTrack]  = None
        self.current_sell_wall: Optional[WallTrack] = None

        self.tape = deque(maxlen=500)  # (ts, side, price, base)
        self.last_signal_ts = 0.0

    def _update_best(self):
        if self.bids: self.best_bid = max(self.bids.keys())
        if self.asks: self.best_ask = min(self.asks.keys())

    def _apply_book(self, bids_upd, asks_upd):
        for px, sz in bids_upd:
            p = float(px); s = float(sz)
            if s<=0: self.bids.pop(p, None)
            else:    self.bids[p]=s
        for px, sz in asks_upd:
            p = float(px); s = float(sz)
            if s<=0: self.asks.pop(p, None)
            else:    self.asks[p]=s
        self._update_best()

    def _apply_trade(self, tr):
        price = float(tr["price"]); base = float(tr["amount"]); side = tr.get("side","")
        ts = time.time()
        self.tape.append((ts, side, price, base))
        if self.current_buy_wall and abs(price - self.current_buy_wall.price) < 1e-12 and side=="sell":
            self.current_buy_wall.record_trade_hit(base)
        if self.current_sell_wall and abs(price - self.current_sell_wall.price) < 1e-12 and side=="buy":
            self.current_sell_wall.record_trade_hit(base)

    def _avg_depth_eur(self, side_dict: Dict[float,float]) -> float:
        total_eur = 0.0; levels=0
        if not side_dict: return 0.0
        # للطلبات: نرتب تنازلياً بالسعر، للعروض: تصاعدياً
        if side_dict is self.bids:
            items = sorted(side_dict.items(), key=lambda x: -x[0])
        else:
            items = sorted(side_dict.items(), key=lambda x: x[0])
        for i,(p,sz) in enumerate(items):
            if i>=DEPTH_LEVELS: break
            total_eur += p*sz; levels+=1
        return (total_eur/levels) if levels>0 else 0.0

    def _detect_wall(self, side: str) -> Optional[Tuple[WallTrack, float]]:
        side_dict = self.bids if side=="buy" else self.asks
        if not side_dict: return None
        avg_eur = self._avg_depth_eur(side_dict)
        # راقب أول DEPTH_LEVELS مستوى
        if side=="buy":
            levels = sorted(side_dict.items(), key=lambda x: -x[0])[:DEPTH_LEVELS]
        else:
            levels = sorted(side_dict.items(), key=lambda x: x[0])[:DEPTH_LEVELS]

        for price, size in levels:
            size_eur = price*size
            if size_eur < WALL_MIN_EUR: 
                continue
            if avg_eur>0 and (size_eur/avg_eur) < WALL_SIZE_RATIO:
                continue
            # مرشح
            track = self.current_buy_wall if side=="buy" else self.current_sell_wall
            if track and abs(track.price-price)<1e-12:
                track.update_size(size)
                return track, avg_eur
            else:
                tr = WallTrack(side, price, size)
                if side=="buy":  self.current_buy_wall = tr
                else:            self.current_sell_wall = tr
                return tr, avg_eur
        return None

    def _tape_speed(self, last_sec=2.0) -> float:
        now = time.time()
        return sum(b for (ts,_,_,b) in self.tape if now-ts<=last_sec)

    def maker_entry_buy_px(self, wall: WallTrack) -> float:
        tgt = px_round_up(wall.price + FRONT_TICKS*self.tick, self.tick)
        if self.best_ask:
            tgt = min(tgt, self.best_ask - self.tick)
        return max(self.tick, tgt)

    def maker_entry_sell_px(self, wall: WallTrack) -> float:
        tgt = px_round_down(wall.price - FRONT_TICKS*self.tick, self.tick)
        if self.best_bid:
            tgt = max(tgt, self.best_bid + self.tick)
        return max(self.tick, tgt)

    async def run_once(self) -> Optional[dict]:
        """
        يدير WebSocket لسوق واحد حتى يولّد إشارة صالحة أو تنتهي المهلة.
        يُعيد dict بالنتيجة أو None إذا لا شيء.
        عند ظهور buy_wall قوي → يُعاد {"type":"follow_buy", "market":..., "price":..., "info":{...}}
        """
        async with websockets.connect(BITVAVO_WS, ping_interval=20, ping_timeout=20) as ws:
            # اشتراك
            sub = {"action":"subscribe","channels":[
                {"name":"book","markets":[self.market]},
                {"name":"trades","markets":[self.market]}
            ]}
            await ws.send(json.dumps(sub))
            snapshot_ok=False
            start_ts=time.time()

            while True:
                raw = await ws.recv()
                msg = json.loads(raw)

                if msg.get("event")=="book":
                    self._apply_book(msg.get("bids",[]), msg.get("asks",[]))
                    snapshot_ok=True
                    self.current_buy_wall = None; self.current_sell_wall=None
                    continue
                if not snapshot_ok:
                    continue

                if msg.get("event")=="bookUpdate":
                    self._apply_book(msg.get("bids",[]), msg.get("asks",[]))

                if msg.get("event")=="trade":
                    for tr in msg.get("trades",[]):
                        self._apply_trade(tr)

                # اكتشاف/تحديث جدران
                bw = self._detect_wall("buy")
                sw = self._detect_wall("sell")

                now=time.time()
                if now - self.last_signal_ts < COOLDOWN_SEC:
                    continue

                speed = self._tape_speed(2.0)

                # follow_buy؟
                if self.current_buy_wall:
                    S, info = self.current_buy_wall.score(self._avg_depth_eur(self.bids))
                    conds = [
                        info["life"] >= STICKY_SEC_MIN,
                        info["hit_ratio"] >= HIT_RATIO_MIN,
                        info["depl_speed"] >= DEPL_SPEED_MIN,
                        info["replenish_ratio"] <= REPLENISH_OK_MAX,
                        S >= SCORE_FOLLOW,
                        speed > 0.0
                    ]
                    if self.current_buy_wall.size <= 1e-8:
                        self.current_buy_wall=None
                    elif all(conds):
                        entry = self.maker_entry_buy_px(self.current_buy_wall)
                        self.last_signal_ts = now
                        return {
                            "type":"follow_buy",
                            "market": self.market,
                            "price": entry,
                            "score": round(S,3),
                            "wall_px": self.current_buy_wall.price,
                            "info": {k:(round(v,4) if isinstance(v,float) else v) for k,v in info.items()}
                        }

                # انتهاء صلاحية جدار قديم
                for w in (self.current_buy_wall, self.current_sell_wall):
                    if w and (now - w.ts_last > MAX_HOLD_SIGNAL):
                        if w.side=="buy": self.current_buy_wall=None
                        else: self.current_sell_wall=None

# ========= مدير المسح المتسلسل عبر عدة أسواق =========
class ExpressManager:
    """
    يدور على MARKETS بالتسلسل. عند أول إشارة follow_buy صحيحة:
    - يرسل POST إلى صقر /hook
    - يوقف المسح ويدخل حالة SIGNAL_SENT وينتظر /ready ليستأنف
    يدعم /scan لتدوير run_id وإعادة الانطلاق فورا.
    """
    def __init__(self, markets):
        self.markets = markets[:]
        self.state = "IDLE"        # IDLE | SCANNING | SIGNAL_SENT
        self.run_id = 0
        self._lock = threading.Lock()
        self.loop = None
        self.thread = None
        self.stop_flag = False

    def set_state(self, s):
        with self._lock:
            self.state = s

    def inc_run(self):
        with self._lock:
            self.run_id += 1
            return self.run_id

    def current_run(self):
        with self._lock:
            return self.run_id

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag = False
        self.thread = threading.Thread(target=self._runner, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag = True

    def _runner(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        if AUTOSCAN_ON_START:
            self.loop.run_until_complete(self.scan_cycle())
        else:
            self.set_state("IDLE")
            tg_send("🟡 Express v6 جاهز — استخدم /scan للبدء.")
            while not self.stop_flag:
                time.sleep(0.2)

    async def scan_cycle(self):
        self.set_state("SCANNING")
        my_run = self.inc_run()
        tg_send(f"🟢 بدء مسح جديد (run {my_run}) — الأسواق: {', '.join(self.markets)}")
        try:
            while not self.stop_flag and self.state=="SCANNING" and my_run==self.current_run():
                for m in self.markets:
                    if self.state!="SCANNING" or my_run!=self.current_run() or self.stop_flag:
                        break
                    surfer = MarketSurfer(m)
                    try:
                        # مهلة تقديرية لكل سوق قبل الانتقال (يمكن ضبطها بزمن خارجي إذا رغبت)
                        result = await asyncio.wait_for(surfer.run_once(), timeout=60)
                    except asyncio.TimeoutError:
                        result = None
                    except Exception as e:
                        tg_send(f"⚠️ خطأ سوق {m}: {e}")
                        result = None

                    if result and result.get("type")=="follow_buy":
                        # أطلق الإشارة إلى صقر
                        ok = self.send_to_saqer(result)
                        # في كل الأحوال: توقّف وانتظر /ready لاستئناف
                        self.set_state("SIGNAL_SENT")
                        info = result.get("info", {})
                        tg_send(
                            f"🚀 إشارة شراء ({m})\n"
                            f"سعر Maker≈ {result['price']}\n"
                            f"Score={result['score']} | life={info.get('life')} | "
                            f"hit={info.get('hit_ratio')} | speed={info.get('depl_speed')} | "
                            f"repl={info.get('replenish_ratio')} | prog={info.get('progress')}\n"
                            f"تم الإرسال إلى صقر: {'OK' if ok else 'FAILED'}\n"
                            f"⏸️ التوقف حتى /ready من صقر."
                        )
                        return
                # لم نجد شيء: كرّر الدورة
        finally:
            # إذا خرج بدون إشارة، ارجع IDLE
            if self.state=="SCANNING":
                self.set_state("IDLE")
                tg_send("ℹ️ انتهاء دورة المسح بدون إشارة (IDLE).")

    def send_to_saqer(self, signal: dict) -> bool:
        coin = signal["market"].split("-")[0]
        data = {"action":"buy", "coin":coin, "tp_eur":TP_EUR, "sl_pct":SL_PCT}
        headers = {"X-Link-Secret": LINK_SECRET} if LINK_SECRET else {}
        try:
            r = requests.post(SAQAR_WEBHOOK+"/hook", json=data, headers=headers, timeout=5)
            return r.status_code>=200 and r.status_code<300
        except Exception as e:
            tg_send(f"⛔ فشل إرسال إشارة لصقر: {e}")
            return False

    # تحكّم خارجي من Flask
    def api_scan(self):
        # أعد التشغيل من جديد
        self.set_state("SCANNING")
        if self.loop and self.loop.is_running():
            # شغّل دورة مسح جديدة على الحدث loop
            asyncio.run_coroutine_threadsafe(self.scan_cycle(), self.loop)
        else:
            # إذا لم تكن حلقة فعّالة، ابدأ الخيط
            self.start()
        return {"ok": True, "state": self.state, "run": self.current_run()}

    def api_ready(self):
        # صقر يقول جاهز — استأنف مسح
        if self.state=="SIGNAL_SENT":
            self.set_state("SCANNING")
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self.scan_cycle(), self.loop)
            else:
                self.start()
        else:
            # لو ما في إشارة سابقة، فقط تأكد من التشغيل
            if self.state!="SCANNING":
                self.set_state("SCANNING")
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.scan_cycle(), self.loop)
                else:
                    self.start()
        return {"ok": True, "state": self.state, "run": self.current_run()}

    def api_health(self):
        return {"ok": True, "state": self.state, "run": self.current_run(), "markets": self.markets}

# ========= Flask API =========
app = Flask(__name__)
manager = ExpressManager(MARKETS)

@app.route("/scan", methods=["POST","GET"])
def http_scan():
    res = manager.api_scan()
    return jsonify(res)

@app.route("/ready", methods=["POST","GET"])
def http_ready():
    # صقر يستدعي هذا بعد البيع/الفشل ليعيد الإقلاع
    reason = request.json.get("reason") if request.is_json else None
    if reason:
        tg_send(f"✅ Ready من صقر (reason={reason}) — استئناف المسح.")
    res = manager.api_ready()
    return jsonify(res)

@app.route("/health", methods=["GET"])
def http_health():
    return jsonify(manager.api_health())

# ========= التشغيل =========
if __name__ == "__main__":
    # ابدأ الخيط الخلفي
    manager.start()
    # سيرفر ويب خفيف
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8081")), threaded=True)