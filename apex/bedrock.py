"""Bedrock — defensive, recession-resilient, equal-weight buy & hold.

The low-risk sleeve: staples, healthcare, auto-parts, discount retail, waste, a
utility and gold — everyday businesses that hold up in downturns. Bought in
equal weight on 2026-05-22 and held. Stateless — the whole track record is
recomputed from fixed share counts + price history each run, then written to
data/bedrock_state.json in the same format the dashboard reads.

Goal: beat inflation comfortably, keep rough pace with the S&P over time, and
hold its ground when the momentum names break.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

import pandas as pd
import yfinance as yf

from . import divs, jsonio
from .paths import DATA_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

STATE = DATA_DIR / "bedrock_state.json"
SEED_DATE = "2026-05-22"
CAPITAL = 100000.0
BENCH = "SPY"            # S&P 500 proxy (has extended hours) — the single benchmark
BENCH_LABEL = "S&P 500"
STRATEGY = "Bedrock · defensive, recession-resilient buy & hold"
REBAL_AMOUNT = 150.0   # tiny monthly trim->add: registers as an Autopilot move,
                       # negligible return drag (drift-sized, not a full rebalance)

TICKERS = ["WMT", "COST", "PG", "KO", "PEP", "CL", "MDLZ",
           "JNJ", "UNH", "ABBV", "MRK", "LLY", "AMGN",
           "AZO", "ORLY", "ROST", "TJX", "DG",
           "WM", "RSG", "NEE", "DUK", "GLD"]

SECTOR = {
    "WMT": "Staples", "COST": "Staples", "PG": "Staples", "KO": "Staples",
    "PEP": "Staples", "CL": "Staples", "MDLZ": "Staples", "DG": "Staples",
    "JNJ": "Healthcare", "UNH": "Healthcare", "ABBV": "Healthcare",
    "MRK": "Healthcare", "LLY": "Healthcare", "AMGN": "Healthcare",
    "AZO": "Auto Parts", "ORLY": "Auto Parts",
    "ROST": "Discount Retail", "TJX": "Discount Retail",
    "WM": "Waste", "RSG": "Waste",
    "NEE": "Utilities", "DUK": "Utilities",
    "GLD": "Gold",
}

DISCLAIMER = ("Paper-tracked. Not investment advice. No brokerage connection — "
              "you execute your own trades.")


def _closeon(s, date):
    s = s.dropna().loc[:date]
    return float(s.iloc[-1]) if len(s) else None


def _daily(end=None):
    df = yf.download(TICKERS + [BENCH], start="2026-05-18", end=end,
                     auto_adjust=False, progress=False, group_by="ticker")
    out = {}
    for t in TICKERS + [BENCH]:
        try:
            out[t] = df[t]["Close"]
        except Exception:
            pass
    d = pd.DataFrame(out)
    d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d")
    return d.sort_index()


def _intraday(shares, end=None):
    start = (pd.Timestamp.now("UTC") - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    start = max(start, SEED_DATE)
    try:
        raw = yf.download(TICKERS, start=start, interval="30m", auto_adjust=False,
                          progress=False, group_by="ticker", prepost=True)
        braw = yf.download([BENCH], start=start, interval="30m", auto_adjust=False,
                           progress=False, group_by="ticker", prepost=True)
    except Exception:
        return []
    cols = {}
    for t in TICKERS:
        try:
            cols[t] = raw[t]["Close"]
        except Exception:
            pass
    try:
        cols[BENCH] = braw["Close"] if "Close" in braw else braw[BENCH]["Close"]
    except Exception:
        pass
    if BENCH not in cols:
        return []
    df = pd.DataFrame(cols).ffill().dropna(subset=[BENCH])
    if df.empty:
        return []
    b0 = float(df[BENCH].iloc[0])
    out = []
    for ts, row in df.iterrows():
        d = ts.strftime("%Y-%m-%d")
        if d < SEED_DATE:
            continue
        val = sum(shares[t] * row[t] for t in TICKERS
                  if t in row and pd.notna(row[t]))
        bp = row[BENCH]
        out.append({"t": ts.strftime("%Y-%m-%d %H:%M"), "value": round(val, 2),
                    "benchmark": round(CAPITAL * bp / b0, 2) if pd.notna(bp) else None})
    # anchor inception at the seed regular close (one point), then forward
    seed_pts = [p for p in out if p["t"][:10] == SEED_DATE]
    if seed_pts:
        reg = [p for p in seed_pts if p["t"][11:] <= "16:00"]
        anchor = reg[-1] if reg else seed_pts[-1]
        out = [anchor] + [p for p in out if p["t"][:10] != SEED_DATE]
    return out


def build():
    daily = _daily()
    seed_px = {t: _closeon(daily[t], SEED_DATE) for t in TICKERS}
    missing = [t for t in TICKERS if not seed_px.get(t)]
    if missing:
        print("WARN missing seed prices:", missing)
    per = CAPITAL / len(TICKERS)
    shares = {t: per / seed_px[t] for t in TICKERS if seed_px.get(t)}
    bench_seed = _closeon(daily[BENCH], SEED_DATE)

    # Only real trading days. On a holiday yfinance can hand back an all-NaN row;
    # including it produced a $0 / -100% curve point and NaN benchmark returns
    # (NaN is truthy in Python, so the old guard let it through).
    dates = [d for d in daily.index if d >= SEED_DATE and pd.notna(daily[BENCH].get(d))]
    held = list(shares)
    if not held or not dates:   # full fetch failure — keep the last good state
        print("WARN bedrock: empty price fetch; keeping existing state.")
        return None
    moves = [{"date": SEED_DATE, "ticker": None, "action": "BUY", "rule": "Seed",
              "rationale": f"Bought {len(held)} defensive, recession-resilient "
                           "names, equal weight. Buy & hold.", "judge": "mechanical", "price": None}]
    trade_days = [{"date": SEED_DATE, "n": len(held), "theme": "Defensive",
                   "closed": [], "changed": [],
                   "opened": [{"ticker": t, "weight": round(1 / len(held), 4),
                               "theme": SECTOR[t]} for t in held]}]

    curve = []
    prev_month = SEED_DATE[:7]
    n_rebal = 0
    for d in dates:
        if d[:7] != prev_month:               # first trading day of a new month
            prev_month = d[:7]
            vals = {t: shares[t] * float(daily[t].get(d)) for t in held
                    if pd.notna(daily[t].get(d))}
            if vals:
                tot = sum(vals.values())
                over = max(vals, key=vals.get)
                under = min(vals, key=vals.get)
                if over != under:
                    po, pu = float(daily[over].get(d)), float(daily[under].get(d))
                    amt = min(REBAL_AMOUNT, vals[over] * 0.9)
                    fo, fu = vals[over] / tot, vals[under] / tot
                    shares[over] -= amt / po
                    shares[under] += amt / pu
                    to_, tu = shares[over] * po / tot, shares[under] * pu / tot
                    n_rebal += 1
                    trade_days.append({"date": d, "n": 2, "theme": "Rebalance",
                        "opened": [], "closed": [], "changed": [
                          {"ticker": over, "from": round(fo, 4), "to": round(to_, 4), "theme": SECTOR[over]},
                          {"ticker": under, "from": round(fu, 4), "to": round(tu, 4), "theme": SECTOR[under]}]})
                    moves.append({"date": d, "ticker": over, "action": "TRIM", "rule": "Rebalance",
                        "rationale": f"Monthly rebalance — trimmed ${amt:.0f} of {over} (top weight) into {under}.",
                        "judge": "mechanical", "price": round(po, 2)})
                    moves.append({"date": d, "ticker": under, "action": "ADD", "rule": "Rebalance",
                        "rationale": f"Monthly rebalance — added ${amt:.0f} to {under} (lowest weight) from {over}.",
                        "judge": "mechanical", "price": round(pu, 2)})
        val = sum(shares[t] * float(daily[t].get(d)) for t in held
                  if pd.notna(daily[t].get(d)))
        bp = daily[BENCH].get(d)
        bval = CAPITAL * bp / bench_seed if pd.notna(bp) and bench_seed else None
        curve.append({"date": d, "value": round(val, 2),
                      "ret": round(val / CAPITAL - 1, 4),
                      "benchmark": round(bval, 2) if bval else None,
                      "benchmark_ret": round(bp / bench_seed - 1, 4) if (pd.notna(bp) and bench_seed) else None,
                      "cash": 0})

    last = dates[-1]
    last_px = {t: float(daily[t].get(last)) for t in held if pd.notna(daily[t].get(last))}
    # A transient empty fetch can leave the latest row all-NaN; walk back to the
    # last day that actually has prices rather than crash on a zero total.
    di = len(dates) - 1
    while not last_px and di > 0:
        di -= 1
        last = dates[di]
        last_px = {t: float(daily[t].get(last)) for t in held if pd.notna(daily[t].get(last))}
    total = sum(shares[t] * last_px[t] for t in last_px)
    if not total:   # truly no data — keep the last good state.json, don't clobber it
        print("WARN bedrock: no price data this run; keeping existing state.")
        return None
    positions, theme_val = [], defaultdict(float)
    for t in held:
        price = last_px.get(t, seed_px[t])
        value = shares[t] * price
        theme_val[SECTOR[t]] += value
        positions.append({"ticker": t, "theme": SECTOR[t], "conviction": "core",
                          "shares": round(shares[t], 3), "avg_entry": round(seed_px[t], 2),
                          "price": round(price, 2), "value": round(value, 2),
                          "drawdown": round(max(0, 1 - price / seed_px[t]), 4),
                          "ret": round(price / seed_px[t] - 1, 4),
                          "thesis": f"Defensive holding ({SECTOR[t]})."})
    for p in positions:
        p["weight"] = round(p["value"] / total, 4) if total else 0
    positions.sort(key=lambda x: -x["value"])
    themes = [{"theme": k, "weight": round(v / total, 4)} for k, v in
              sorted(theme_val.items(), key=lambda kv: -kv[1])]

    moves = list(reversed(moves))
    trade_days = list(reversed(trade_days))

    intraday = _intraday(shares)

    # Dividend cash this buy & hold book would have collected (shown beside NAV,
    # never inside it — NAV is price return, like Autopilot).
    div_total, div_per = divs.dividends_since(shares, SEED_DATE)

    f = curve[-1]
    state = {
        "meta": {"initial_deposit": CAPITAL, "start_date": SEED_DATE,
                 "last_date": last, "mode": "forward", "strategy": STRATEGY,
                 "total_value": round(total, 2), "cash": 0,
                 "total_return": f["ret"], "benchmark_return": f["benchmark_ret"],
                 "alpha": round((f["ret"] or 0) - (f["benchmark_ret"] or 0), 4),
                 "num_trades": n_rebal, "benchmark_label": BENCH_LABEL,
                 "risk": "Low", "disclaimer": DISCLAIMER},
        "curve": curve, "positions": positions, "themes": themes,
        "moves": moves, "trade_days": trade_days, "intraday": intraday,
        "dividends": {"total": div_total, "per": div_per},
    }
    jsonio.dump(state, STATE)
    print(f"Built {STATE.name}: {len(curve)} days, {len(positions)} positions, "
          f"return {f['ret']*100:+.1f}% vs {BENCH_LABEL} {(f['benchmark_ret'] or 0)*100:+.1f}%.")
    return state


if __name__ == "__main__":
    build()
