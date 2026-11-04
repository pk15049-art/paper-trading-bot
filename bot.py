"""Paper-trading intraday bot, US stocks, Alpaca PAPER account. Virtual budget $1,000.
Runs every 5 min via GitHub Actions; writes docs/state.json for the public dashboard.
Entry: 5-min EMA9 crosses above EMA21, RSI 50-72, price above VWAP, volume above 20-bar avg.
Exit (checked every run): stop -1.2%; at +1% stop moves to breakeven; then trails 1.5% below
the highest price seen; take profit +4%; EMA cross-down exit; everything flat by 15:50 ET.
Risk: 20% of budget per trade (up to 5 trades at once), no new entries after -3% day."""
import os, json, datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

KEY, SECRET = os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]
SYMBOLS = ["SPY","QQQ","IWM","AAPL","MSFT","NVDA","AMD","TSLA","META","AMZN","GOOGL","NFLX",
           "AVGO","PLTR","COIN","UBER","JPM","BA","SHOP","MU"]
BUDGET, SLOT, MAX_POS = 1000.0, 0.20, 5
STOP, BE_AT, TRAIL, TARGET, DAY_LOSS = 0.012, 0.01, 0.015, 0.04, 0.03
ET, IST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")
STATE = "docs/state.json"
tc = TradingClient(KEY, SECRET, paper=True)
dc = StockHistoricalDataClient(KEY, SECRET)

def load():
    try: return json.load(open(STATE))
    except Exception:
        return {"budget": BUDGET, "cash": BUDGET, "positions": {}, "trades": [], "equity": [],
                "day": None, "day_start": BUDGET, "started": dt.datetime.now(IST).isoformat()}

def ind(df):
    df["ema9"] = df.close.ewm(span=9, adjust=False).mean()
    df["ema21"] = df.close.ewm(span=21, adjust=False).mean()
    d = df.close.diff(); up = d.clip(lower=0).rolling(14).mean(); dn = (-d.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn)
    tp = (df.high + df.low + df.close) / 3
    day = df.index.get_level_values("timestamp").tz_convert(ET).date
    df["vwap"] = (tp * df.volume).groupby(day).cumsum() / df.volume.groupby(day).cumsum()
    df["vavg"] = df.volume.rolling(20).mean()
    return df

def fill_price(order_id, fallback):
    try:
        o = tc.get_order_by_id(order_id)
        return float(o.filled_avg_price) if o.filled_avg_price else fallback
    except Exception: return fallback

def sell(st, s, price, reason, now):
    p = st["positions"].pop(s)
    st.setdefault("cooldown", {})[s] = int(now.timestamp())
    try: o = tc.close_position(s); price = fill_price(o.id, price)
    except Exception as e: print("close err", s, e)
    pnl = (price - p["entry"]) * p["qty"]
    st["cash"] += p["qty"] * price
    st["trades"].insert(0, {"symbol": s, "side": "EXIT", "ts": int(now.timestamp()), "time": now.astimezone(IST).strftime("%d %b %H:%M"),
        "entry": round(p["entry"], 2), "exit": round(price, 2), "qty": round(p["qty"], 4),
        "pnl": round(pnl, 2), "pnl_pct": round((price / p["entry"] - 1) * 100, 2), "reason": reason})
    print(f"EXIT {s} @{price:.2f} pnl {pnl:+.2f} ({reason})")

def main():
    st = load(); now = dt.datetime.now(ET)
    clock = tc.get_clock()
    st["last_run"] = now.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    st["market_open"] = bool(clock.is_open)
    st["next_open"] = clock.next_open.astimezone(IST).strftime("%d %b, %I:%M %p IST")
    if clock.is_open:
        today = now.date().isoformat()
        if st.get("day") != today:
            st["day"] = today
            st["day_start"] = st["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in st["positions"].values())
        bars = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMBOLS,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute), start=now - dt.timedelta(days=4),
                feed=DataFeed.IEX)).df
        have = set(bars.index.get_level_values(0))
        eod = now.hour == 15 and now.minute >= 50
        # manage open positions
        for s in list(st["positions"]):
            p = st["positions"][s]
            if s not in have: continue
            df = ind(bars.loc[[s]].copy()); b, a = df.iloc[-1], df.iloc[-2]
            px = float(b.close); p["last"] = px; p["high"] = max(p["high"], float(b.high))
            if p["high"] >= p["entry"] * (1 + BE_AT): p["stop"] = max(p["stop"], p["entry"])
            p["stop"] = max(p["stop"], p["high"] * (1 - TRAIL)) if p["high"] >= p["entry"] * (1 + BE_AT) else p["stop"]
            if eod: sell(st, s, px, "Day end", now)
            elif float(b.low) <= p["stop"]: sell(st, s, max(p["stop"], float(b.low)) if px > p["stop"] else px, "Stop/trail", now)
            elif px >= p["entry"] * (1 + TARGET): sell(st, s, px, "Target", now)
            elif a.ema9 >= a.ema21 and b.ema9 < b.ema21: sell(st, s, px, "Trend reversed", now)
        equity = st["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in st["positions"].values())
        halted = equity < st["day_start"] * (1 - DAY_LOSS)
        if not eod and not halted and now.hour < 15:
            for s in SYMBOLS:
                if len(st["positions"]) >= MAX_POS: break
                if s in st["positions"] or s not in have: continue
                if now.timestamp() - st.get("cooldown", {}).get(s, 0) < 1800: continue
                df = ind(bars.loc[[s]].copy())
                if len(df) < 30: continue
                a, b = df.iloc[-2], df.iloc[-1]
                if (a.ema9 <= a.ema21 and b.ema9 > b.ema21 and 50 < b.rsi < 72 and b.close > b.vwap
                        and b.volume > b.vavg):
                    amt = round(min(st["budget"] * SLOT, st["cash"]), 2)
                    if amt < 20: continue
                    try:
                        o = tc.submit_order(MarketOrderRequest(symbol=s, notional=amt, side=OrderSide.BUY,
                                            time_in_force=TimeInForce.DAY))
                    except Exception as e: print("buy err", s, e); continue
                    import time; time.sleep(2)
                    px = fill_price(o.id, float(b.close)); qty = amt / px
                    st["cash"] -= amt
                    st["positions"][s] = {"qty": qty, "entry": px, "stop": px * (1 - STOP), "high": px,
                        "last": px, "time": now.astimezone(IST).strftime("%d %b %H:%M")}
                    st["trades"].insert(0, {"symbol": s, "side": "ENTRY", "ts": int(now.timestamp()), "time": now.astimezone(IST).strftime("%d %b %H:%M"),
                        "entry": round(px, 2), "qty": round(qty, 4), "amount": amt})
                    print(f"ENTRY {s} ${amt} @{px:.2f}")
        # candles for chart: SPY + open positions + symbols traded in last 2 days
        recent = {t["symbol"] for t in st["trades"] if t.get("ts", 0) > now.timestamp() - 2 * 86400}
        show = ["SPY"] + sorted((set(st["positions"]) | recent) - {"SPY"})
        cut = now - dt.timedelta(days=2)
        st["candles"] = {}
        for s in show:
            if s not in have: continue
            b = bars.loc[s]; b = b[b.index >= cut]
            st["candles"][s] = [[int(ts.timestamp()), round(float(r.open), 2), round(float(r.high), 2),
                                 round(float(r.low), 2), round(float(r.close), 2)] for ts, r in b.iterrows()]
        equity = st["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in st["positions"].values())
        st["equity"].append([now.astimezone(IST).strftime("%Y-%m-%d %H:%M"), round(equity, 2)])
        st["equity"] = st["equity"][-3000:]; st["trades"] = st["trades"][:300]
        st["halted"] = halted
    else:
        try:
            recent = {t["symbol"] for t in st["trades"] if t.get("ts", 0) > now.timestamp() - 3 * 86400}
            show = ["SPY"] + sorted((set(st["positions"]) | recent) - {"SPY"})
            bars = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=show,
                    timeframe=TimeFrame(5, TimeFrameUnit.Minute), start=now - dt.timedelta(days=5),
                    feed=DataFeed.IEX)).df
            st["candles"] = {}
            for s in show:
                if s not in set(bars.index.get_level_values(0)): continue
                b = bars.loc[s]
                b = b[b.index >= b.index.max() - dt.timedelta(hours=30)]
                st["candles"][s] = [[int(ts.timestamp()), round(float(r.open), 2), round(float(r.high), 2),
                                     round(float(r.low), 2), round(float(r.close), 2)] for ts, r in b.iterrows()]
        except Exception as e: print("candle err", e)
    eq = st["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in st["positions"].values())
    exits = [t for t in st["trades"] if t["side"] == "EXIT"]
    st["summary"] = {"equity": round(eq, 2), "pnl": round(eq - st["budget"], 2),
        "pnl_pct": round((eq / st["budget"] - 1) * 100, 2), "cash": round(st["cash"], 2),
        "invested": round(sum(p["qty"] * p.get("last", p["entry"]) for p in st["positions"].values()), 2),
        "today_pnl": round(eq - st.get("day_start", st["budget"]), 2),
        "closed_trades": len(exits), "wins": sum(1 for t in exits if t["pnl"] > 0)}
    os.makedirs("docs", exist_ok=True)
    json.dump(st, open(STATE, "w"), indent=1)
    print("Summary", st["summary"], "market_open", st["market_open"])

def save(msg="bot update"):
    import subprocess as sp
    cmds = ["git add docs/state.json", f'git commit -q -m "{msg}"', "git pull -q --rebase", "git push -q"]
    for c in cmds: sp.run(c, shell=True)

if __name__ == "__main__":
    import time
    if os.environ.get("LOOP") != "1":
        main()
    else:
        t0 = time.time(); last_save = 0; was_open = False
        while time.time() - t0 < 345 * 60:
            try: main()
            except Exception as e: print("run err", e)
            is_open = json.load(open(STATE)).get("market_open")
            if time.time() - last_save > 290 or not is_open:
                save(); last_save = time.time()
            if not is_open: break
            was_open = True
            time.sleep(60)
