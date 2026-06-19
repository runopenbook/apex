"""Dividend cash a held book would have collected since inception.

NAV across the dashboards is deliberately PRICE return (auto_adjust=False) — it
excludes dividends, to mirror how Autopilot tracks a pilot on the price of held
positions. Dividends are still real money, so we surface them separately (a little
"you'd have earned $X" box) without folding them into NAV.

Approximation: uses the supplied (current) share counts, so it's exact for buy &
hold and close enough for a slow-rotating book. A failed download returns zero —
it must never break a daily/refresh run.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf


def dividends_since(holdings, start, end=None):
    """holdings: {ticker: shares}. Returns (total_cash, [{ticker, amount, dps}])
    for cash dividends with ex-date >= start."""
    tickers = [t for t, sh in holdings.items() if sh]
    if not tickers:
        return 0.0, []
    try:
        df = yf.download(tickers, start=start, end=end, auto_adjust=False,
                         actions=True, progress=False, group_by="ticker")
    except Exception:
        return 0.0, []
    total, per = 0.0, []
    for t in tickers:
        try:
            dv = df[t]["Dividends"] if t in df.columns.get_level_values(0) else df["Dividends"]
            dps = float(dv[dv > 0].sum())
        except Exception:
            dps = 0.0
        if dps > 0:
            amt = holdings[t] * dps
            total += amt
            per.append({"ticker": t, "amount": round(amt, 2), "dps": round(dps, 4)})
    per.sort(key=lambda x: -x["amount"])
    return round(total, 2), per
