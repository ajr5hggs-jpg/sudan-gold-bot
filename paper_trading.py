"""Paper-only XAUUSD strategy - MODIFIED RSI 65/35 FAST"""
from __future__ import annotations
import json, logging, math, os, tempfile, threading, time
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo
import yfinance as yf

YAHOO_TICKERS = ("XAUUSD=X", "GC=F", "PAXG-USD")
PAPER_STATE_PATH = Path(__file__).parent / "bot_data" / "paper_accounts.json"
BAR_SECONDS = 60 * 60
YAHOO_PERIOD = "60d"
YAHOO_INTERVAL = "1h"
PAPER_CHECK_SECONDS = 2 * 60
MIN_HISTORY_BARS = 100
STARTING_BALANCE_USD = 10_000.0
RISK_FRACTION = 0.02
STOP_POINTS = 150
TAKE_PROFIT_POINTS = 300
POINT_SIZE_USD = 0.01
MAX_QUOTE_AGE_SECONDS = 2 * BAR_SECONDS
REQUEST_TIMEOUT_SECONDS = 20
NEW_YORK = ZoneInfo("America/New_York")
logger = logging.getLogger("paper-trader")

def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped
class PaperTradingError(RuntimeError): pass
@dataclass(frozen=True)
class Candle:
    timestamp: int; open: float; high: float; low: float; close: float; volume: float = 0.0
@dataclass(frozen=True)
class MarketData:
    candles: list[Candle]; price: float; source: str; yahoo_ticker: str | None = None
def is_market_open(moment=None):
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None: current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(NEW_YORK)
    clock = local.timetz().replace(tzinfo=None)
    if local.weekday()==5: return False
    if local.weekday()==6: return clock >= wall_time(18,0)
    if local.weekday()==4: return clock < wall_time(17,0)
    return not (wall_time(17,0) <= clock < wall_time(18,0))
def ema_series(values, period):
    result=[None]*len(values)
    if len(values)<period: return result
    current=sum(values[:period])/period
    result[period-1]=current
    alpha=2.0/(period+1)
    for i in range(period,len(values)):
        current=values[i]*alpha+current*(1-alpha)
        result[i]=current
    return result
def rsi_series(values, period=14):
    result=[None]*len(values)
    if len(values)<=period: return result
    changes=[values[i]-values[i-1] for i in range(1,len(values))]
    gains=[max(c,0) for c in changes]
    losses=[max(-c,0) for c in changes]
    avg_gain=sum(gains[:period])/period
    avg_loss=sum(losses[:period])/period
    def calc(g,l):
        if l==0: return 100.0 if g>0 else 50.0
        return 100.0-100.0/(1.0+g/l)
    result[period]=calc(avg_gain,avg_loss)
    for idx in range(period,len(changes)):
        avg_gain=(avg_gain*(period-1)+gains[idx])/period
        avg_loss=(avg_loss*(period-1)+losses[idx])/period
        result[idx+1]=calc(avg_gain,avg_loss)
    return result
def crossover_signal(index, fast_ema, slow_ema, rsi):
    if index<1 or index>=len(fast_ema): return None
    cf=fast_ema[index]; cs=slow_ema[index]; cr=rsi[index]
    if None in (cf,cs,cr): return None
    if cr < 35: return "long"
    if cr > 65: return "short"
    return None
def position_size_ounces(balance_usd, stop_distance_usd):
    if balance_usd<=0 or stop_distance_usd<=0: return 0.0
    return math.floor((balance_usd*0.02/stop_distance_usd)*10000)/10000
def _finite_float(v):
    n=float(v)
    if not math.isfinite(n): raise ValueError()
    return n
def _field_series(frame,name,ticker):
    s=frame[name]
    if getattr(s,"ndim",1)>1:
        cols=getattr(s,"columns",[])
        s=s[ticker] if ticker in cols else s.iloc[:,0]
    return s
def _parse_yahoo_frame(frame,ticker,now_epoch):
    if frame is None or frame.empty: raise PaperTradingError("no candles")
    opens=_field_series(frame,"Open",ticker)
    highs=_field_series(frame,"High",ticker)
    lows=_field_series(frame,"Low",ticker)
    closes=_field_series(frame,"Close",ticker)
    volumes=_field_series(frame,"Volume",ticker)
    candles=[]; latest_price=None; latest_ts=None
    for idx, tv in enumerate(frame.index):
        moment=tv.to_pydatetime()
        if moment.tzinfo is None: moment=moment.replace(tzinfo=timezone.utc)
        ts=int(moment.timestamp())
        try:
            op=_finite_float(opens.iloc[idx]); hi=_finite_float(highs.iloc[idx]); lo=_finite_float(lows.iloc[idx]); cl=_finite_float(closes.iloc[idx])
            try: vol=_finite_float(volumes.iloc[idx])
            except: vol=0.0
        except: continue
        if cl<=0 or hi<lo: continue
        if latest_ts is None or ts>=latest_ts: latest_ts=ts; latest_price=cl
        if ts % BAR_SECONDS !=0 or ts+BAR_SECONDS>now_epoch: continue
        candles.append(Candle(ts,op,hi,lo,cl,vol))
    if latest_price is None: raise PaperTradingError("no price")
    if now_epoch-latest_ts>MAX_QUOTE_AGE_SECONDS: raise PaperTradingError("price old")
    candles.sort(key=lambda c:c.timestamp)
    if len(candles)<MIN_HISTORY_BARS: raise PaperTradingError("few candles")
    src="Yahoo "+ticker
    return MarketData(candles,latest_price,src,ticker)
def _fetch_yahoo(ticker,now_epoch):
    frame=yf.download(ticker,period=YAHOO_PERIOD,interval=YAHOO_INTERVAL,progress=False,auto_adjust=False,actions=False,threads=False,prepost=True,timeout=REQUEST_TIMEOUT_SECONDS)
    return _parse_yahoo_frame(frame,ticker,now_epoch)
def fetch_market_data(pref=None):
    now=time.time()
    order=[pref] if pref else []
    order.extend(t for t in YAHOO_TICKERS if t not in order)
    for ticker in order:
        try: return _fetch_yahoo(ticker,now)
        except: continue
    raise PaperTradingError("no data")
def _new_account():
    return {"enabled":False,"enabled_at":0.0,"balance_usd":10000.0,"position":None,"last_processed_candle":0,"timeframe_seconds":BAR_SECONDS,"last_price":None,"last_price_at":0.0,"source":"","closed_trades":0}
class PaperTradingEngine:
    def __init__(self,notify,state_path=PAPER_STATE_PATH,market_data_loader=fetch_market_data):
        self.notify=notify; self.state_path=state_path; self.market_data_loader=market_data_loader
        self.accounts=self._load_accounts(); self._lock=threading.RLock()
        self.last_market_data=None; self.preferred_yahoo_ticker=None; self._check_requested=False
    @property
    def check_requested(self): return self._check_requested
    def _load_accounts(self):
        try: raw=self.state_path.read_text()
        except: return {}
        loaded=json.loads(raw); acc={}
        for k,v in loaded.items():
            n=_new_account(); n.update(v); acc[str(k)]=n
        return acc
    def _save(self):
        self.state_path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w",dir=self.state_path.parent,delete=False) as tf:
            json.dump(self.accounts,tf,indent=2,allow_nan=False); tf.write("\n"); p=tf.name
        os.replace(p,self.state_path)
    def _account(self,chat_id):
        k=str(chat_id)
        if k not in self.accounts: self.accounts[k]=_new_account(); self._save()
        return self.accounts[k]
    @synchronized
    def start(self,chat_id):
        a=self._account(chat_id); a["enabled"]=True; a["enabled_at"]=time.time()
        if self.last_market_data and self.last_market_data.candles: a["last_processed_candle"]=self.last_market_data.candles[-1].timestamp
        self._check_requested=True; self._save()
        return "✅ تم التشغيل RSI 65/35 - فحص كل دقيقتين"
    @synchronized
    def stop(self,chat_id):
        a=self._account(chat_id); a["enabled"]=False; self._save(); return "تم الإيقاف"
    def _unrealized_pnl(self,pos,price):
        return (price-pos["entry_price"])*pos["size_oz"] if pos["side"]=="long" else (pos["entry_price"]-price)*pos["size_oz"]
    def _close_position(self,chat_id,account,price,reason,notify_chat=True):
        pos=account["position"]; pnl=self._unrealized_pnl(pos,price)
        account["balance_usd"]+=pnl; account["closed_trades"]+=1
        side="شراء" if pos["side"]=="long" else "بيع"
        msg=f"🔒 إغلاق {side} | {reason}\nالسعر: ${price:.2f}\nالربح: ${pnl:.2f}\nالرصيد: ${account['balance_usd']:.2f}"
        account["position"]=None
        if notify_chat: self.notify(chat_id,msg)
        return msg
    def _open_position(self,chat_id,account,candle,signal,source,notify_chat=True):
        price=candle.close; stop_dist=150*0.01
        size=position_size_ounces(account["balance_usd"],stop_dist)
        if size<=0: return "رصيد لا يكفي"
        if signal=="long": stop=price-stop_dist; target=price+300*0.01; side_ar="شراء"
        else: stop=price+stop_dist; target=price-300*0.01; side_ar="بيع"
        account["position"]={"side":signal,"entry_price":price,"stop_price":stop,"target_price":target,"size_oz":size,"opened_at":candle.timestamp}
        msg=f"📈 إشارة {side_ar} RSI 65/35\nالسعر: ${price:.2f}\nالحجم: {size} oz\nوقف: ${stop:.2f} | هدف: ${target:.2f}\n{source}"
        if notify_chat: self.notify(chat_id,msg)
        return msg
    @synchronized
    def balance_message(self,chat_id):
        a=self._account(chat_id); bal=float(a["balance_usd"]); pos=a.get("position")
        if pos:
            price=a.get("last_price") or pos["entry_price"]
            pnl=self._unrealized_pnl(pos,float(price))
            return f"الرصيد: ${bal:.2f}\nالحقوق: ${bal+pnl:.2f}\nالصفقة: {pos['side']} {pos['size_oz']} oz\nعائم: ${pnl:.2f}"
        return f"الرصيد: ${bal:.2f}\nلا توجد صفقات"
    @synchronized
    def close_all(self,chat_id):
        a=self._account(chat_id)
        if not a.get("position"): return "لا توجد صفقة"
        price=a.get("last_price")
        if price is None: return "لا يوجد سعر"
        return self._close_position(chat_id,a,float(price),"يدوي",False)
    @synchronized
    def trade_now(self,chat_id):
        a=self._account(chat_id)
        if a.get("position"): return "لديك صفقة مفتوحة"
        if not is_market_open(): return "السوق مغلق"
        market=self.market_data_loader(self.preferred_yahoo_ticker)
        self.last_market_data=market
        if market.yahoo_ticker: self.preferred_yahoo_ticker=market.yahoo_ticker
        a["last_price"]=market.price
        closes=[c.close for c in market.candles]
        sig=crossover_signal(len(closes)-1,ema_series(closes,20),ema_series(closes,50),rsi_series(closes,14))
        if sig is None:
            r=rsi_series(closes,14); return f"لا إشارة - RSI الحالي: {r[-1]:.1f} (نحتاج <35 شراء أو >65 بيع)"
        return self._open_position(chat_id,a,market.candles[-1],sig,market.source,False)
    @synchronized
    def run_cycle(self,moment=None):
        self._check_requested=False
        if not is_market_open(moment): return
        if not any(x.get("enabled") or x.get("position") for x in self.accounts.values()): return
        market=self.market_data_loader(self.preferred_yahoo_ticker)
        self.last_market_data=market
        if market.yahoo_ticker: self.preferred_yahoo_ticker=market.yahoo_ticker
        self.process_market_data(market,moment)
    @synchronized
    def process_market_data(self,market,moment=None):
        current=moment or datetime.now(timezone.utc)
        candles=sorted(market.candles,key=lambda c:c.timestamp)
        closes=[c.close for c in candles]
        fast=ema_series(closes,20); slow=ema_series(closes,50); rsi=rsi_series(closes,14)
        for k,acc in self.accounts.items():
            cid=int(k); acc["last_price"]=market.price; acc["last_price_at"]=time.time()
            last=int(acc.get("last_processed_candle") or 0)
            if last==0: acc["last_processed_candle"]=candles[-1].timestamp; continue
            pos=acc.get("position")
            if pos:
                p=market.price
                if pos["side"]=="long" and (p<=pos["stop_price"] or p>=pos["target_price"]): self._close_position(cid,acc,p,"وقف/هدف")
                elif pos["side"]=="short" and (p>=pos["stop_price"] or p<=pos["target_price"]): self._close_position(cid,acc,p,"وقف/هدف")
            if not acc.get("enabled"): continue
            if candles[-1].timestamp<=last: continue
            sig=crossover_signal(len(candles)-1,fast,slow,rsi)
            if sig:
                self._open_position(cid,acc,candles[-1],sig,market.source,True)
                acc["last_processed_candle"]=candles[-1].timestamp
        self._save()
    def _check_live_exit(self,chat_id,account,price): pass
