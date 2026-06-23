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

import statistics

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
    # Drop garbage pre/post-market ticks: thin/illiquid names (esp. int'l ETFs)
    # print bad quotes at 4am that lurch then revert. Flag *extended-hours* bars
    # that sit far from the day's median, scaled to the book's OWN volatility
    # (median abs deviation) so a defensive book's 4% spike is caught while a
    # moonshot's real 4% move is kept. Floor at 2.5% so a flat day with a couple
    # of garbage ticks still gets cleaned. Regular-hours bars are ALWAYS kept —
    # we never hide a real session move.
    if len(sess) >= 6:
        vals = [p["value"] for p in sess]
        med = statistics.median(vals)
        mad = statistics.median([abs(v - med) for v in vals])
        tol = max(5 * mad, med * 0.025)
        sess = [p for p in sess
                if ("09:30" <= p["t"][11:] <= "16:00") or abs(p["value"] - med) <= tol]
    if not sess:                       # market closed / fetch failed: keep the last close
        last = curve[-1]
        base.append({"t": last["date"] + " 16:00", "value": last["value"],
                     "benchmark": last["benchmark"]})
    return base + sess
