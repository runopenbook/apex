"""Honest strategy lab — mechanical, point-in-time, forward-runnable engines.

No hand-picked names: each engine selects from a fixed universe using only data
available at each rebalance date, so the backtest behaves like the live system
would. Includes transaction costs and a trend filter for downside protection.

Engines:
  - ETF rotation      : dual momentum over sector/asset ETFs (cleanest, ~zero
                        name-selection bias — sectors are a fixed partition).
  - Stock momentum    : rank a broad large-cap list, hold the strongest.
  - Thematic momentum : same mechanic over a structural-theme universe.

Benchmarks: SPY buy & hold, and equal-weight of the stock universe (no momentum).

Honesty caveats (printed in the report): the stock/thematic universes still carry
*survivorship* bias (today's surviving names; bankrupt names aren't free to source)
and the thematic list carries mild theme-selection bias. The ETF engine is the
bias-clean reference.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd
import yfinance as yf

from .paths import DATA_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LAB_JSON = DATA_DIR / "lab.json"

ETF_UNIVERSE = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
                "SMH", "ITA", "GLD", "TLT", "IYR"]

STOCK_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "QCOM", "TXN", "MU", "INTC", "CSCO",
    "ORCL", "ADBE", "CRM", "GOOGL", "META", "AMZN", "NFLX",
    "LMT", "NOC", "RTX", "GD", "BA", "CAT", "DE", "HON", "GE", "ETN", "EMR",
    "XOM", "CVX", "COP", "SLB", "EOG",
    "JPM", "BAC", "GS", "MS", "V", "MA", "AXP",
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO",
    "WMT", "COST", "HD", "MCD", "NKE", "SBUX", "PG", "KO", "PEP", "DIS",
    "TSLA", "LIN", "UPS", "UNP",
]

THEMATIC_UNIVERSE = [
    # AI / semis
    "NVDA", "AMD", "AVGO", "MU", "TSM", "QCOM", "MRVL", "ASML", "LRCX", "AMAT",
    "KLAC", "SMCI", "PLTR", "SNOW",
    # defense / nuclear
    "LMT", "NOC", "RTX", "GD", "BWXT", "LHX", "HII",
    # energy / power
    "CEG", "VST", "NEE", "XOM", "CVX", "ETN", "GEV",
    # space
    "RKLB", "ASTS", "LUNR",
    # commodity
    "GLD", "FCX", "NEM",
]


def load_prices(tickers, start, end):
    tickers = list(dict.fromkeys(tickers))
    df = yf.download(tickers, start=start, end=end, auto_adjust=True,
                     progress=False, group_by="ticker")
    out = {}
    for t in tickers:
        try:
            s = df[t]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if len(s) > 200:
            out[t] = s
    p = pd.DataFrame(out)
    p.index = pd.to_datetime(p.index)
    return p.sort_index()


def run_rotation(prices, *, lookback=126, top_n=5, trend_ma=200, cost=0.001):
    """Monthly dual-momentum rotation. Returns (equity Series, stats dict)."""
    daily_ret = prices.pct_change()
    sma = prices.rolling(trend_ma, min_periods=trend_ma // 2).mean()
    mom = prices / prices.shift(lookback) - 1

    period = prices.index.to_period("M")
    rebal = set(prices.index[~period.duplicated(keep="first")])

    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    cur = pd.Series(0.0, index=prices.columns)
    turnovers = []
    for d in prices.index:
        if d in rebal:
            m = mom.loc[d].dropna()
            if len(m):
                picks = m.sort_values(ascending=False).index[:top_n]
                neww = pd.Series(0.0, index=prices.columns)
                for tk in picks:
                    s = sma.loc[d, tk]
                    if pd.notna(s) and prices.loc[d, tk] > s:   # trend filter
                        neww[tk] = 1.0 / top_n
                turnovers.append((neww - cur).abs().sum())
                cur = neww
        weights.loc[d] = cur

    port = (weights.shift(1) * daily_ret).sum(axis=1)
    cost_hits = pd.Series(0.0, index=prices.index)
    j = 0
    for d in prices.index:
        if d in rebal and j < len(turnovers):
            cost_hits.loc[d] = turnovers[j] * cost
            j += 1
    port = port - cost_hits
    equity = (1 + port.fillna(0)).cumprod()
    return equity, port, np.mean(turnovers) if turnovers else 0.0


def equal_weight_hold(prices, *, cost=0.001):
    """Equal-weight the available names, rebalanced monthly. (No momentum.)"""
    daily_ret = prices.pct_change()
    avail = prices.notna()
    period = prices.index.to_period("M")
    rebal = set(prices.index[~period.duplicated(keep="first")])
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    cur = pd.Series(0.0, index=prices.columns)
    for d in prices.index:
        if d in rebal:
            names = avail.loc[d][avail.loc[d]].index
            cur = pd.Series(0.0, index=prices.columns)
            if len(names):
                cur[names] = 1.0 / len(names)
        weights.loc[d] = cur
    port = (weights.shift(1) * daily_ret).sum(axis=1)
    equity = (1 + port.fillna(0)).cumprod()
    return equity, port


def metrics(equity, port_ret):
    eq = equity.dropna()
    total = eq.iloc[-1] - 1
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    vol = port_ret.std() * np.sqrt(252)
    sharpe = (cagr / vol) if vol else 0
    return {"total": round(total, 4), "cagr": round(cagr, 4),
            "maxdd": round(dd, 4), "vol": round(vol, 4), "sharpe": round(sharpe, 2)}


def _curve(equity, capital=100000, step=5):
    eq = equity.dropna()
    eq = eq.iloc[::step]
    return [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v) * capital, 2)}
            for d, v in eq.items()]


def run_race(start="2019-01-01", end="2026-06-18", capital=100000):
    print(f"Loading universes ({start} -> {end})...")
    etf_p = load_prices(ETF_UNIVERSE + ["SPY"], start, end)
    stock_p = load_prices(STOCK_UNIVERSE + ["SPY"], start, end)
    them_p = load_prices(THEMATIC_UNIVERSE, start, end)

    spy = etf_p["SPY"]
    spy_eq = spy / spy.iloc[0]
    spy_ret = spy.pct_change()

    results = []

    def add(name, color, equity, port, turnover=None):
        results.append({"name": name, "color": color,
                        "metrics": metrics(equity, port),
                        "turnover": round(turnover, 3) if turnover is not None else None,
                        "curve": _curve(equity, capital)})

    e, r, to = run_rotation(etf_p.drop(columns=["SPY"]), top_n=5)
    add("ETF rotation", "#6aa3ff", e, r, to)
    e, r, to = run_rotation(stock_p.drop(columns=["SPY"]), top_n=10)
    add("Stock momentum", "#26d07c", e, r, to)
    e, r, to = run_rotation(them_p, top_n=10)
    add("Thematic momentum", "#b07bff", e, r, to)
    e, r = equal_weight_hold(stock_p.drop(columns=["SPY"]))
    add("Equal-weight stocks (hold)", "#e8b341", e, r)
    add("S&P 500 (SPY)", "#8a96ab", spy_eq, spy_ret)

    state = {"window": {"start": start, "end": end},
             "strategies": results,
             "note": "Mechanical point-in-time momentum/trend rotation, monthly, "
                     "with a 200d trend filter and 10bps turnover cost. ETF rotation "
                     "is the bias-clean reference; stock/thematic carry residual "
                     "survivorship bias. Signal-copying engine omitted (needs live "
                     "alt-data, not honestly backtestable)."}
    LAB_JSON.write_text(json.dumps(state, indent=2))

    print(f"\n{'Strategy':30}{'Total':>9}{'CAGR':>8}{'MaxDD':>8}{'Vol':>7}{'Sharpe':>8}{'Turn':>7}")
    print("-" * 77)
    for s in results:
        m = s["metrics"]
        t = f"{s['turnover']:.2f}" if s["turnover"] is not None else "-"
        print(f"{s['name']:30}{m['total']*100:+8.1f}%{m['cagr']*100:+7.1f}%"
              f"{m['maxdd']*100:+7.1f}%{m['vol']*100:6.1f}%{m['sharpe']:8.2f}{t:>7}")
    print("-" * 77)
    print(f"\nSaved {LAB_JSON}")
    return state


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-06-18")
    a = ap.parse_args()
    run_race(a.start, a.end)
