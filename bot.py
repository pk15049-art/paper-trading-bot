"""Paper-trading intraday bot for US stocks on Alpaca (paper account, virtual $100k).
Runs once per call (GitHub Actions every 5 min in market hours). No real money.
Strategy: 5-min bars, EMA9/EMA21 cross-up + RSI 50-70 + price above VWAP -> buy with
bracket order (stop -1%, target +2%). Exit on EMA cross-down. Flat by 15:50 ET.
Risk: 10% of equity per trade, max 5 positions, stop new entries after -2% day."""
import os, datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

KEY, SECRET = os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]
SYMBOLS = ["SPY","QQQ","AAPL","MSFT","NVDA","AMD","TSLA","META","AMZN","GOOGL"]
RISK_PCT, MAX_POS, STOP, TARGET, DAY_LOSS = 0.10, 5, 0.01, 0.02, 0.02
ET = ZoneInfo("America/New_York")
tc = TradingClient(KEY, SECRET, paper=True)
dc = StockHistoricalDataClient(KEY, SECRET)

def indicators(df):
    df["ema9"] = df.close.ewm(span=9, adjust=False).mean()
    df["ema21"] = df.close.ewm(span=21, adjust=False).mean()
    d = df.close.diff(); up = d.clip(lower=0).rolling(14).mean(); dn = (-d.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn)
    tp = (df.high + df.low + df.close) / 3
    day = df.index.get_level_values("timestamp").tz_convert(ET).date
    df["vwap"] = (tp * df.volume).groupby(day).cumsum() / df.volume.groupby(day).cumsum()
    return df

def main():
    clock = tc.get_clock()
    if not clock.is_open:
        print("Market closed"); return
    now = dt.datetime.now(ET)
    acct = tc.get_account()
    equity, last_eq = float(acct.equity), float(acct.last_equity)
    positions = {p.symbol: p for p in tc.get_all_positions()}
    if now.hour == 15 and now.minute >= 50:
        tc.cancel_orders(); tc.close_all_positions(cancel_orders=True)
        print("EOD: closed all"); return
    bars = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMBOLS,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute), start=now - dt.timedelta(days=3),
            feed=DataFeed.IEX)).df
    open_syms = {o.symbol for o in tc.get_orders()}
    halted = equity < last_eq * (1 - DAY_LOSS)
    for s in SYMBOLS:
        if s not in bars.index.get_level_values(0): continue
        df = indicators(bars.loc[[s]].copy())
        if len(df) < 30: continue
        a, b = df.iloc[-2], df.iloc[-1]
        cross_up = a.ema9 <= a.ema21 and b.ema9 > b.ema21
        cross_dn = a.ema9 >= a.ema21 and b.ema9 < b.ema21
        if s in positions and cross_dn:
            tc.close_position(s); print(f"EXIT {s} @~{b.close:.2f} (EMA cross down)"); continue
        if (s not in positions and s not in open_syms and not halted and len(positions) < MAX_POS
                and cross_up and 50 < b.rsi < 70 and b.close > b.vwap and now.hour < 15):
            qty = int(equity * RISK_PCT // b.close)
            if qty < 1: continue
            tc.submit_order(MarketOrderRequest(symbol=s, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(b.close * (1 + TARGET), 2)),
                stop_loss=StopLossRequest(stop_price=round(b.close * (1 - STOP), 2))))
            positions[s] = True
            print(f"ENTRY {s} x{qty} @~{b.close:.2f}")
    print(f"Equity ${equity:,.2f} (day start ${last_eq:,.2f}){' HALTED' if halted else ''}")

if __name__ == "__main__":
    main()
