"""CLI: seed the book, run a backtest, run a single forward day, export state.

Usage (from project root):
  py -m apex.runner init        [--start YYYY-MM-DD] [--capital 100000]
  py -m apex.runner backtest    [--start YYYY-MM-DD] [--end YYYY-MM-DD]
  py -m apex.runner forward                          # one live day, today
  py -m apex.runner export                           # rebuild dashboard JSON
  py -m apex.runner run         [--start ...]        # init + backtest + export
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from . import ledger, data, engine, export, review
from .paths import CONFIG_DIR

MA_WINDOW = 20


def _load_config():
    book = json.loads((CONFIG_DIR / "book.json").read_text())
    framework = json.loads((CONFIG_DIR / "framework.json").read_text())
    return book, framework


def _all_tickers(book, framework):
    instr = framework["instruments"]
    holdings = [p["ticker"] for p in book["positions"]]
    macro = [instr["benchmark"], instr["gold_spot"], instr["crude"]]
    return holdings, macro


def _load_events():
    path = CONFIG_DIR / "events.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def cmd_init(start=None, capital=None, book=None, framework=None, verbose=True):
    if book is None or framework is None:
        book, framework = _load_config()
    holdings, macro = _all_tickers(book, framework)
    capital = capital or book["initial_deposit"]
    start = start or (pd.Timestamp.now("UTC") - pd.Timedelta(days=365 * 2)).strftime("%Y-%m-%d")

    df = data.download_closes(holdings + macro, start=start)
    df = df.ffill().dropna(subset=holdings, how="any")
    if df.empty:
        raise SystemExit("No price data returned for the start window.")
    seed_date = df.index[0]
    row = df.loc[seed_date]

    ledger.reset_db()
    with ledger.connect() as conn:
        ledger.init_db(conn)
        ledger.set_meta(conn, "framework", framework)
        ledger.set_meta(conn, "initial_deposit", capital)
        ledger.set_meta(conn, "start_date", seed_date)
        ledger.set_meta(conn, "mode", "backtest")
        cash = capital
        for p in book["positions"]:
            price = float(row[p["ticker"]])
            alloc = capital * p["target_weight"]
            shares = alloc / price
            flags = {k: v for k, v in p.items()
                     if k in ("is_gold_hedge", "catalyst_gated")}
            ledger.upsert_position(conn, p["ticker"], p["theme"], p["conviction"],
                                   p["target_weight"], thesis=p.get("thesis", ""),
                                   flags=flags, shares=shares, avg_entry=price)
            cash -= alloc
            ledger.log_transaction(conn, seed_date, p["ticker"], "BUY", shares,
                                   price, -alloc, "Seed",
                                   f"Initial position: {p['conviction']} conviction, "
                                   f"target {p['target_weight']*100:.0f}%.")
        ledger.set_cash(conn, cash)
        # No-cash policy: deploy leftover cash at seed so the book starts ~100% invested.
        if framework.get("policy", {}).get("fully_invested"):
            seed_prices = {t: float(row[t]) for t in row.index if pd.notna(row[t])}
            engine.sweep_cash(conn, seed_date, seed_prices,
                              framework["sizing"].get("soft_cap_single", 0.20),
                              capital, log=False)
            cash = ledger.get_cash(conn)
        ledger.set_meta(conn, "benchmark_high", float(row[framework["instruments"]["benchmark"]]))
        ledger.set_meta(conn, "last_review_quarter", review.quarter_label(seed_date))
        ledger.snapshot_equity(conn, seed_date, capital, cash,
                               benchmark=float(row[framework["instruments"]["benchmark"]]),
                               gold=float(row[framework["instruments"]["gold_spot"]]),
                               crude=float(row[framework["instruments"]["crude"]]))
        ledger.log_decision(conn, seed_date, "BUY",
                            f"Book seeded with {len(book['positions'])} positions, "
                            f"{cash/capital*100:.0f}% cash.", rule="Seed",
                            judge="mechanical")
    if verbose:
        print(f"Initialized on {seed_date} with ${capital:,.0f} "
              f"({len(book['positions'])} positions).")
    return seed_date


def cmd_backtest(start=None, end=None, framework=None, verbose=True):
    events_all = _load_events()
    with ledger.connect() as conn:
        seed_date = ledger.get_meta(conn, "start_date")
        framework = framework or ledger.get_meta(conn, "framework")
        holdings = [p["ticker"] for p in ledger.all_positions(conn)]
    macro = [framework["instruments"]["benchmark"],
             framework["instruments"]["gold_spot"], framework["instruments"]["crude"]]
    start = start or seed_date

    df = data.download_closes(holdings + macro, start=start, end=end)
    df = df.ffill()
    ma = df[holdings].rolling(MA_WINDOW, min_periods=5).mean()

    dates = [d for d in df.index if d > seed_date]
    if verbose:
        print(f"Replaying {len(dates)} trading days "
              f"({dates[0] if dates else '-'} to {dates[-1] if dates else '-'})...")

    prev_q = review.quarter_label(seed_date)
    with ledger.connect() as conn:
        for i, date in enumerate(df.index):
            if date <= seed_date:
                continue
            q = review.quarter_label(date)
            if q != prev_q:
                ledger.log_decision(conn, date, "REVIEW",
                                    f"Quarterly review {q}: allocation held — strategic "
                                    "reviews are a live-mode judgment (not backtested to "
                                    "avoid look-ahead).", rule="Quarterly Review",
                                    judge="review")
                prev_q = q
            closes = {t: float(df.loc[date, t]) for t in df.columns
                      if pd.notna(df.loc[date, t])}
            prev = df.iloc[df.index.get_loc(date) - 1]
            prev_closes = {t: float(prev[t]) for t in df.columns if pd.notna(prev[t])}
            ma_map = {t: (float(ma.loc[date, t]) if pd.notna(ma.loc[date, t]) else None)
                      for t in holdings}
            engine.run_day(conn, date, closes, prev_closes, ma_map,
                           mode="heuristic", events=events_all.get(date, {}))
        last = df.index[-1]
        ledger.set_meta(conn, "last_date", last)
        ledger.set_meta(conn, "last_review_quarter", review.quarter_label(last))
        ledger.set_meta(conn, "last_prices",
                        {t: float(df.loc[last, t]) for t in df.columns
                         if pd.notna(df.loc[last, t])})
    if verbose:
        print("Backtest complete.")


def cmd_forward(force=False):
    """Run one live trading day.

    Phase 1 (plan): if any holding hit a news-judgment trigger today and a verdict
    isn't on file yet, queue the questions to data/pending_judgments.json and STOP
    without touching the ledger. Claude Code fills data/judgments.json, then you
    re-run. Phase 2 (commit): with all verdicts present, execute + commit the day.
    """
    from . import judge
    book, framework = _load_config()
    holdings, macro = _all_tickers(book, framework)
    events_all = _load_events()
    df = data.download_closes(
        holdings + macro,
        start=(pd.Timestamp.now("UTC") - pd.Timedelta(days=40)).strftime("%Y-%m-%d"))
    df = df.ffill()
    ma = df[holdings].rolling(MA_WINDOW, min_periods=5).mean()
    date = df.index[-1]
    prev = df.iloc[-2]
    closes = {t: float(df.loc[date, t]) for t in df.columns if pd.notna(df.loc[date, t])}
    prev_closes = {t: float(prev[t]) for t in df.columns if pd.notna(prev[t])}
    ma_map = {t: (float(ma.loc[date, t]) if pd.notna(ma.loc[date, t]) else None)
              for t in holdings}
    events = events_all.get(date, {})

    with ledger.connect() as conn:
        if ledger.get_meta(conn, "last_committed_date") == date and not force:
            print(f"{date} already committed. Re-run with --force to redo "
                  "(note: trades are not auto-reversed).")
            return
        # Quarterly strategic review gate (runs before the daily rules).
        if review.review_due(conn, date):
            q = review.quarter_label(date)
            verdict = review.load_verdict(q)
            if verdict is None:
                review.write_request(conn, date, q, closes, framework)
                print(f"\n[{date}] Quarterly review due for {q}. Reconsider the themes:\n"
                      "  Claude Code: read data/pending_review.json, write new targets "
                      f"to data/review.json under \"{q}\", then re-run `forward`.")
                return
            moves = review.apply_review(conn, date, q, verdict, closes, framework)
            ledger.set_meta(conn, "last_review_quarter", q)
            print(f"Applied quarterly review {q}: {moves} rebalance move(s).")
        needed = engine.plan_day(conn, date, closes, prev_closes, ma_map,
                                 events=events, news_provider=data.get_news)

    if needed:
        print(f"\n[{date}] {len(needed)} judgment(s) needed before committing:")
        for n in needed:
            print(f"  - {n['ticker']} ({n['kind']}): day {n['day_change']*100:+.1f}%, "
                  f"drawdown {n['drawdown']*100:.0f}%, {n['headlines']} headlines")
        print("\nClaude Code: read data/pending_judgments.json, judge the news, "
              "write verdicts to data/judgments.json, then re-run `forward`.")
        return

    with ledger.connect() as conn:
        summary = engine.run_day(conn, date, closes, prev_closes, ma_map,
                                 mode="claude", events=events,
                                 news_provider=data.get_news)
        ledger.set_meta(conn, "last_date", date)
        ledger.set_meta(conn, "last_prices", closes)
        ledger.set_meta(conn, "mode", "forward")
        ledger.set_meta(conn, "last_committed_date", date)
    judge.clear_pending()
    export.export_state()
    print(f"Forward day {date} committed: {summary['moves']} move(s), "
          f"value ${summary['total_value']:,.0f}.")


def main():
    ap = argparse.ArgumentParser(prog="apex.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "backtest", "forward", "export", "run"):
        s = sub.add_parser(name)
        if name in ("init", "backtest", "run"):
            s.add_argument("--start")
        if name in ("backtest", "run"):
            s.add_argument("--end")
        if name in ("init", "run"):
            s.add_argument("--capital", type=float)
        if name == "forward":
            s.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.cmd == "init":
        cmd_init(args.start, args.capital)
    elif args.cmd == "backtest":
        cmd_backtest(args.start, args.end)
    elif args.cmd == "forward":
        cmd_forward(force=getattr(args, "force", False))
    elif args.cmd == "export":
        export.export_state()
    elif args.cmd == "run":
        cmd_init(args.start, args.capital)
        cmd_backtest(None, args.end)
        export.export_state()


if __name__ == "__main__":
    main()
