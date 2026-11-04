"""NSE intraday PAPER bot (simulated, no broker). Virtual capital Rs 1,000 (fractional qty simulated). Data: Yahoo Finance 5-min bars.
Same rules as the US bot: EMA9/EMA21 cross-up, RSI 50-72, above VWAP, volume above 20-bar average.
Exits: stop -1.2%, breakeven at +1%, 1.5% trailing, target +4%, trend reversal, square-off 15:15 IST.
Up to 5 trades at once, 20% capital each, fractional quantity (simulation), approx Zerodha-style intraday charges deducted.
Strongest signals (highest volume surge) are taken first."""
import os, json, time, datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd, yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
SYMS = ["RELIANCE","HDFCBANK","ICICIBANK","INFY","TCS","SBIN","AXISBANK","KOTAKBANK","LT","BHARTIARTL",
        "ITC","MARUTI","BAJFINANCE","HINDUNILVR","SUNPHARMA","TITAN","ADANIENT","M&M","NTPC","TATASTEEL",
        "HCLTECH","WIPRO","ONGC","POWERGRID","JSWSTEEL"]
BUDGET, SLOT, MAX_POS = 1000.0, 0.20, 5
STOP, BE_AT, TRAIL, TARGET, DAY_LOSS = 0.012, 0.01, 0.015, 0.04, 0.03
DAY_LOCK, TIGHT_TRAIL = 0.03, 0.0075  # after +3% day: no new entries, trail tightens to 0.75%
STATE = "docs/nse_state.json"

def load():
    try:
        st = json.load(open(STATE))
        if st.get("budget") == BUDGET or st.get("trades"): return st
    except Exception: pass
    if True:
        return {"budget": BUDGET, "cash": BUDGET, "positions": {}, "trades": [], "equity": [], "day": None,
                "day_start": BUDGET, "charges": 0.0, "currency": "INR", "started": dt.datetime.now(IST).isoformat()}

def charges(buy_val, sell_val):
    brk = min(20, 0.0003 * buy_val) + min(20, 0.0003 * sell_val)
    stt = 0.00025 * sell_val
    other = 0.0000345 * (buy_val + sell_val) + 0.00003 * buy_val  # exchange + stamp
    gst = 0.18 * (brk + 0.0000345 * (buy_val + sell_val))
    return round(brk + stt + other + gst, 2)

def ind(df):
    df = df.copy()
    df["ema9"] = df.Close.ewm(span=9, adjust=False).mean()
    df["ema21"] = df.Close.ewm(span=21, adjust=False).mean()
    d = df.Close.diff(); up = d.clip(lower=0).rolling(14).mean(); dn = (-d.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn)
    tp = (df.High + df.Low + df.Close) / 3; day = df.index.date
    df["vwap"] = (tp * df.Volume).groupby(day).cumsum() / df.Volume.groupby(day).cumsum().replace(0, 1)
    df["vavg"] = df.Volume.rolling(20).mean()
    return df

def fetch(now):
    for i in range(3):
        out = _fetch()
        if len(out) >= 10: return out
        print("data retry", i, len(out)); time.sleep(15 * (i + 1))
    return out

def _fetch():
    try:
        raw = yf.download([s + ".NS" for s in SYMS], period="5d", interval="5m", group_by="ticker",
                      progress=False, threads=False, auto_adjust=False)
    except Exception as e:
        print("download err", e); return {}
    out = {}
    for s in SYMS:
        try:
            df = raw[s + ".NS"].dropna(subset=["Close"])
            df.index = df.index.tz_convert(IST)
            if len(df): out[s] = df
        except Exception: pass
    return out

def tstr(now): return now.strftime("%d %b %H:%M")

def sell(st, s, px, reason, now):
    p = st["positions"].pop(s)
    st.setdefault("cooldown", {})[s] = int(now.timestamp())
    sv = p["qty"] * px; ch = charges(p["qty"] * p["entry"], sv)
    pnl = sv - p["qty"] * p["entry"] - ch
    st["cash"] += sv - ch; st["charges"] = round(st.get("charges", 0) + ch, 2)
    st["trades"].insert(0, {"symbol": s, "side": "EXIT", "ts": int(now.timestamp()), "time": tstr(now),
        "entry": round(p["entry"], 2), "exit": round(px, 2), "qty": p["qty"], "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / (p["qty"] * p["entry"]) * 100, 2), "reason": reason})
    print(f"EXIT {s} @{px:.2f} pnl {pnl:+.2f} ({reason})")

def value(st): return st["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in st["positions"].values())

def main():
    st = load(); now = dt.datetime.now(IST)
    st["last_run"] = now.strftime("%d %b %Y, %I:%M %p IST")
    wk = now.weekday() < 5
    in_hours = wk and dt.time(9, 15) <= now.time() <= dt.time(15, 30)
    data = fetch(now) if (in_hours or not st.get("candles")) else {}
    # holiday check: no bar today after 9:25
    fresh = any(len(df) and df.index[-1].date() == now.date() for df in data.values())
    # if data failed, trust the clock (don't stop the loop); holiday = data OK but no bar today
    is_open = in_hours and (fresh or not data or now.time() < dt.time(9, 25))
    st["data_ok"] = bool(data)
    st["market_open"] = bool(is_open)
    st["next_open"] = "Mon–Fri 9:15 AM IST"
    if is_open and data:
        today = now.date().isoformat()
        if st.get("day") != today: st["day"] = today; st["day_start"] = value(st)
        eod = now.time() >= dt.time(15, 15)
        locked = value(st) >= st["day_start"] * (1 + DAY_LOCK); st["profit_locked"] = locked
        trail = TIGHT_TRAIL if locked else TRAIL
        for s in list(st["positions"]):
            if s not in data: continue
            df = ind(data[s]); b = df.iloc[-1]
            done = df[df.index + pd.Timedelta(minutes=5) <= now]
            px = float(b.Close); p = st["positions"][s]; p["last"] = px; p["high"] = max(p["high"], float(b.High))
            if p["high"] >= p["entry"] * (1 + BE_AT):
                p["stop"] = max(p["stop"], p["entry"], p["high"] * (1 - trail))
            if eod: sell(st, s, px, "Day end", now)
            elif float(b.Low) <= p["stop"]: sell(st, s, p["stop"] if px > p["stop"] else px, "Stop/trail", now)
            elif px >= p["entry"] * (1 + TARGET): sell(st, s, px, "Target", now)
            elif len(done) > 2 and done.ema9.iloc[-2] >= done.ema21.iloc[-2] and done.ema9.iloc[-1] < done.ema21.iloc[-1]:
                sell(st, s, px, "Trend reversed", now)
        halted = value(st) < st["day_start"] * (1 - DAY_LOSS); st["halted"] = halted
        if not halted and not locked and now.time() < dt.time(14, 45):
            cands = []
            for s, raw in data.items():
                if s in st["positions"] or now.timestamp() - st.get("cooldown", {}).get(s, 0) < 1800: continue
                df = ind(raw); df = df[df.index + pd.Timedelta(minutes=5) <= now]
                if len(df) < 30 or df.index[-1].date() != now.date(): continue
                a, b = df.iloc[-2], df.iloc[-1]
                if (a.ema9 <= a.ema21 and b.ema9 > b.ema21 and 50 < b.rsi < 72 and b.Close > b.vwap
                        and b.Volume > b.vavg):
                    cands.append((b.Volume / max(b.vavg, 1), s, float(raw.Close.iloc[-1])))
            for _, s, px in sorted(cands, reverse=True):
                if len(st["positions"]) >= MAX_POS: break
                amt = min(st["budget"] * SLOT, st["cash"])
                if amt < st["budget"] * 0.05: continue
                qty = round(amt / px, 4)
                st["cash"] -= qty * px
                st["positions"][s] = {"qty": qty, "entry": px, "stop": px * (1 - STOP), "high": px, "last": px, "time": tstr(now)}
                st["trades"].insert(0, {"symbol": s, "side": "ENTRY", "ts": int(now.timestamp()), "time": tstr(now),
                    "entry": round(px, 2), "qty": qty, "amount": round(qty * px, 2)})
                print(f"ENTRY {s} x{qty} @{px:.2f}")
        st["equity"].append([now.strftime("%Y-%m-%d %H:%M"), round(value(st), 2)])
        st["equity"] = st["equity"][-5000:]; st["trades"] = st["trades"][:300]
    if data:
        recent = {t["symbol"] for t in st["trades"] if t.get("ts", 0) > now.timestamp() - 3 * 86400}
        show = ["RELIANCE"] + sorted((set(st["positions"]) | recent) - {"RELIANCE"})
        st["candles"] = {}
        for s in show:
            if s not in data: continue
            df = data[s]; df = df[df.index >= df.index.max() - pd.Timedelta(hours=30)]
            st["candles"][s] = [[int(ts.timestamp()), round(float(r.Open), 2), round(float(r.High), 2),
                                 round(float(r.Low), 2), round(float(r.Close), 2)] for ts, r in df.iterrows()]
    eq = value(st); exits = [t for t in st["trades"] if t["side"] == "EXIT"]
    st["summary"] = {"equity": round(eq, 2), "pnl": round(eq - st["budget"], 2),
        "pnl_pct": round((eq / st["budget"] - 1) * 100, 2), "cash": round(st["cash"], 2),
        "invested": round(eq - st["cash"], 2), "today_pnl": round(eq - st.get("day_start", st["budget"]), 2),
        "closed_trades": len(exits), "wins": sum(1 for t in exits if t["pnl"] > 0), "charges": st.get("charges", 0)}
    os.makedirs("docs", exist_ok=True); json.dump(st, open(STATE, "w"), indent=1)
    print("Summary", st["summary"], "market_open", st["market_open"])

def save():
    import subprocess as sp
    for c in ["git add docs/nse_state.json", 'git commit -q -m "nse update"', "git pull -q --rebase", "git push -q"]:
        sp.run(c, shell=True)

if __name__ == "__main__":
    if os.environ.get("LOOP") != "1": main()
    else:
        t0 = time.time(); last = 0
        while time.time() - t0 < 345 * 60:
            try: main()
            except Exception as e: print("run err", e)
            op = json.load(open(STATE)).get("market_open")
            if time.time() - last > 290 or not op: save(); last = time.time()
            if not op: break
            time.sleep(120)
