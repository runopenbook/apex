"""Scout the currently-hot leaders across the themes — the selection engine for a
concentrated thematic-momentum portfolio.

Strategy in one line: hold the strongest-trending names across hot themes, exit
when a name breaks its trend, rescan and rotate on a schedule. This module does
the scan: rank candidates by recent momentum, keep only those in a confirmed
uptrend, and build a concentrated book with some theme variety.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CANDIDATES = {
    "AI / Semis": ["NVDA", "AVGO", "AMD", "MU", "MRVL", "SMCI", "TSM", "ARM",
                   "ANET", "DELL", "MSFT", "GOOGL", "META", "PLTR"],
    "Defense / Nuclear": ["LMT", "NOC", "RTX", "GD", "BWXT", "LDOS", "HII",
                          "OKLO", "SMR", "LEU"],
    "Energy / Power": ["CEG", "VST", "GEV", "TLN", "NRG", "NEE", "ETN", "FSLR",
                       "XOM", "CVX"],
    "Crypto / Fintech": ["COIN", "MSTR", "HOOD", "SOFI", "SQ", "NU", "MARA", "RIOT"],
    "Space": ["RKLB", "ASTS", "LUNR"],
    "Other momentum": ["TSLA", "NFLX", "AXON", "CRWD", "PANW", "NOW", "UBER"],
}


def _theme_of(ticker):
    for theme, names in CANDIDATES.items():
        if ticker in names:
            return theme
    return "?"


def scan(end=None):
    tickers = sorted({t for names in CANDIDATES.values() for t in names})
    df = yf.download(tickers, start="2024-06-01", end=end, auto_adjust=True,
                     progress=False, group_by="ticker")
    rows = []
    for t in tickers:
        try:
            s = df[t]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if len(s) < 130:
            continue
        price = s.iloc[-1]
        ret_6m = price / s.iloc[-127] - 1 if len(s) >= 127 else np.nan
        ret_3m = price / s.iloc[-64] - 1 if len(s) >= 64 else np.nan
        ret_1m = price / s.iloc[-22] - 1 if len(s) >= 22 else np.nan
        ma200 = s.tail(200).mean()
        ma50 = s.tail(50).mean()
        rows.append({
            "ticker": t, "theme": _theme_of(t), "price": round(price, 2),
            "ret_6m": ret_6m, "ret_3m": ret_3m, "ret_1m": ret_1m,
            "above_200": price > ma200, "above_50": price > ma50,
            # momentum score: blend of 3m and 6m, only meaningful if trending up
            "score": np.nanmean([ret_6m, ret_3m]),
        })
    out = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    return out


def pick_book(scan_df, n=10, per_theme_cap=3):
    """Concentrated, winners-first, with light theme variety."""
    picks, theme_count = [], {}
    for _, r in scan_df.iterrows():
        if not r["above_200"]:      # only confirmed uptrends — 'hot'
            continue
        if theme_count.get(r["theme"], 0) >= per_theme_cap:
            continue
        picks.append(r)
        theme_count[r["theme"]] = theme_count.get(r["theme"], 0) + 1
        if len(picks) >= n:
            break
    return pd.DataFrame(picks)


def main():
    print("Scanning current momentum leaders across your themes...\n")
    s = scan()
    print(f"{'#':>2} {'Ticker':6} {'Theme':18} {'Price':>9} {'6mo':>8} {'3mo':>8} "
          f"{'1mo':>8} {'Trend':>6}")
    print("-" * 74)
    for i, r in s.iterrows():
        trend = "UP" if r["above_200"] else "down"
        print(f"{i+1:>2} {r['ticker']:6} {r['theme']:18} {r['price']:>9.2f} "
              f"{r['ret_6m']*100:>+7.0f}% {r['ret_3m']*100:>+7.0f}% "
              f"{r['ret_1m']*100:>+7.0f}% {trend:>6}")

    print("\n" + "=" * 74)
    print("PROPOSED AGGRESSIVE CONCENTRATED BOOK (hottest, in-uptrend, max 3/theme):")
    print("=" * 74)
    book = pick_book(s, n=10, per_theme_cap=3)
    w = 1.0 / len(book)
    for _, r in book.iterrows():
        print(f"  {r['ticker']:6} {r['theme']:18} 6mo {r['ret_6m']*100:>+5.0f}%  "
              f"-> target {w*100:.0f}%")
    print(f"\n  {len(book)} names, ~{w*100:.0f}% each (equal-weight start).")


if __name__ == "__main__":
    main()
