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


def _clean_ticks(out):
    """Per-day garbage filter: drop thin pre/post-market prints that sit far from
    the day's median, scaled to that day's own volatility (MAD). Regular-hours
    bars (09:30-16:00) are always kept — never hide a real session move."""
    by_day = {}
    for p in out:
        by_day.setdefault(p["t"][:10], []).append(p)
    keep = []
    for pts in by_day.values():
        if len(pts) >= 6:
            vals = [p["value"] for p in pts]
            med = statistics.median(vals)
            mad = statistics.median([abs(v - med) for v in vals])
            tol = max(5 * mad, med * 0.025)
            pts = [p for p in pts if ("09:30" <= p["t"][11:] <= "16:00")
                   or abs(p["value"] - med) <= tol]
        keep.extend(pts)
    keep.sort(key=lambda p: p["t"])
    return keep


def _launch_anchor(out, launch):
    """The book is seeded at the launch *regular close*, so it has no real intraday
    before then. Keep just that close bar on launch day as the $seed anchor."""
    lp = [p for p in out if p["t"][:10] == launch]
    if not lp:
        return out
    reg = [p for p in lp if p["t"][11:] <= "16:00"]
    anchor = reg[-1] if reg else lp[-1]
    return [anchor] + [p for p in out if p["t"][:10] != launch]


def history(curve, book_by_date, bench, bench_seed, capital, days=58):
    """Honest dense intraday across the book's whole life — the consistent,
    detailed builder. Values the holdings held DURING each day (pre-close trades)
    at 30-min bars, so a book whose holdings change is valued with the book it
    actually held at that moment (never back-valuing today's names to the past).

    Daily closes cover days older than Yahoo's ~2-month 30-min window; 30-min bars
    cover the recent window. Falls back to daily-only on any fetch failure so the
    full history always renders.

    book_by_date: {date: {"h": {ticker: shares}, "cash": cash}} — the pre-trade
                  book for each simulated day, including the seed.
    """
    def dailypts(pred):
        return [{"t": c["date"] + " 16:00", "value": c["value"], "benchmark": c["benchmark"]}
                for c in curve if pred(c["date"])]
    if not curve or not book_by_date:
        return dailypts(lambda d: True)
    launch = min(book_by_date)
    window = (pd.Timestamp.now("UTC") - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    cut = max(window, launch)
    base = dailypts(lambda d: d < cut)                 # older history, one point/day
    tks = sorted({t for v in book_by_date.values() for t in v["h"]})
    if not tks:
        return base + dailypts(lambda d: d >= cut)
    try:
        raw = yf.download(tks + [bench], start=cut, interval="30m", auto_adjust=False,
                          progress=False, group_by="ticker", prepost=True)
        multi = len(tks) + 1 > 1
        cols = {}
        for t in tks + [bench]:
            try:
                cols[t] = raw[t]["Close"] if multi else raw["Close"]
            except Exception:
                pass
        df = pd.DataFrame(cols).ffill().dropna(subset=[bench]) if bench in cols else None
    except Exception:
        df = None
    if df is None or df.empty:
        return base + dailypts(lambda d: d >= cut)     # fetch failed: daily fallback
    keys = sorted(book_by_date)
    dense = []
    for ts, row in df.iterrows():
        d = ts.strftime("%Y-%m-%d")
        if d < cut:
            continue
        bk = book_by_date.get(d)
        if bk is None:                                 # holiday / skipped day: carry prior book
            prior = [x for x in keys if x <= d]
            if not prior:
                continue
            bk = book_by_date[prior[-1]]
        bp = row[bench]
        val = bk["cash"] + sum(sh * row[t] for t, sh in bk["h"].items()
                               if t in row and pd.notna(row[t]))
        dense.append({"t": ts.strftime("%Y-%m-%d %H:%M"), "value": round(float(val), 2),
                      "benchmark": round(capital * bp / bench_seed, 2) if pd.notna(bp) else None})
    dense = _clean_ticks(dense)
    if launch >= window:
        dense = _launch_anchor(dense, launch)
    return base + dense
