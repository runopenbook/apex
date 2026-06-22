"""Live intraday for the actively-managed books (Bedrock, Slipstream, Ignition).

Their holdings change over time, so a seed-anchored intraday curve of the
CURRENT holdings would be dishonest. Instead the engines stitch:

    intraday = [daily curve as one 16:00 point/day] + [today's live session]

so it starts at the true seed (the headline's since-inception math stays
correct, no +0%/+47% glitches) and the 1D view has today's session to draw —
without ever back-valuing today's holdings to the start. Today's session is the
current holdings, which ARE today's holdings (trades execute at the close), so
that part is exact too.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf


def session(shares, bench, bench_seed, capital, after_date, days=4):
    """Recent (today's) intraday points for the CURRENT holdings — only bars
    STRICTLY AFTER `after_date` (the last daily close) so they append to the
    daily curve with no overlap. Benchmark normalised to the real seed."""
    tks = list(shares)
    if not tks:
        return []
    start = (pd.Timestamp.now("UTC") - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        raw = yf.download(tks, start=start, interval="30m", auto_adjust=False,
                          progress=False, group_by="ticker", prepost=True)
        braw = yf.download([bench], start=start, interval="30m", auto_adjust=False,
                           progress=False, group_by="ticker", prepost=True)
    except Exception:
        return []
    cols = {}
    for t in tks:
        try:
            cols[t] = raw[t]["Close"] if len(tks) > 1 else raw["Close"]
        except Exception:
            pass
    try:
        cols[bench] = braw["Close"] if "Close" in braw else braw[bench]["Close"]
    except Exception:
        pass
    if bench not in cols or not cols:
        return []
    df = pd.DataFrame(cols).ffill().dropna(subset=[bench])
    if df.empty:
        return []
    out = []
    for ts, row in df.iterrows():
        if ts.strftime("%Y-%m-%d") <= after_date:      # don't overlap the daily curve
            continue
        val = sum(shares[t] * row[t] for t in tks if t in row and pd.notna(row[t]))
        bp = row[bench]
        out.append({"t": ts.strftime("%Y-%m-%d %H:%M"), "value": round(val, 2),
                    "benchmark": round(capital * bp / bench_seed, 2) if pd.notna(bp) else None})
    return out


def stitch(curve, shares, bench, bench_seed, capital):
    """Daily closes (one 16:00 point per *closed* day) + the latest day's live
    intraday session.

    The engines write a provisional point for the current day into `curve`, so
    curve[-1]["date"] is usually today. Passing that as `after_date` made the
    session — which keeps only bars STRICTLY AFTER it — drop all of today's bars,
    leaving a flat open->close line. So the live session owns the latest day, and
    the daily closes cover only the days before it (no overlap, no double 16:00).
    """
    if not curve:
        return []
    base = [{"t": c["date"] + " 16:00", "value": c["value"], "benchmark": c["benchmark"]}
            for c in curve[:-1]]
    after = curve[-2]["date"] if len(curve) >= 2 else curve[0]["date"]
    sess = session(shares, bench, bench_seed, capital, after)
    if not sess:                       # market closed / fetch failed: keep the last close
        last = curve[-1]
        base.append({"t": last["date"] + " 16:00", "value": last["value"],
                     "benchmark": last["benchmark"]})
    return base + sess
