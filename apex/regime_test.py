"""Regime test: run the v2 framework across different market cycles and compare
it to (a) buy-and-hold of the same book and (b) the S&P 500.

The whole point of the v1->v2 finding was that patience wins in a bull. The open
question is whether the defensive thesis-break exit earns its keep in a *bear*.
So we test fixed 2-year windows including the 2022 drawdown and the COVID crash.

Uses a reduced book of long-history names (no CEG/RKLB, which only IPO'd in
2021-22) so every window holds the same names and is comparable.
"""
from __future__ import annotations

import json
import sys

import pandas as pd
import yfinance as yf

from . import ledger, runner, export
from .paths import CONFIG_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REGIME_BOOK = {
    "initial_deposit": 100000,
    "positions": [
        {"ticker": "NVDA", "theme": "AI / Semiconductors", "conviction": "high",   "target_weight": 0.16, "thesis": "AI compute."},
        {"ticker": "AMD",  "theme": "AI / Semiconductors", "conviction": "high",   "target_weight": 0.12, "thesis": "AI #2."},
        {"ticker": "MU",   "theme": "AI / Semiconductors", "conviction": "medium", "target_weight": 0.10, "thesis": "Memory."},
        {"ticker": "XOM",  "theme": "Energy Transition + Commodity", "conviction": "medium", "target_weight": 0.12, "thesis": "Oil."},
        {"ticker": "IAU",  "theme": "Energy Transition + Commodity", "conviction": "medium", "target_weight": 0.10, "thesis": "Gold.", "is_gold_hedge": True},
        {"ticker": "LMT",  "theme": "Defense / Nuclear", "conviction": "medium", "target_weight": 0.10, "thesis": "Prime."},
        {"ticker": "NOC",  "theme": "Defense / Nuclear", "conviction": "medium", "target_weight": 0.08, "thesis": "Prime."},
        {"ticker": "BWXT", "theme": "Defense / Nuclear", "conviction": "lower",  "target_weight": 0.06, "thesis": "Nuclear."},
        {"ticker": "ETN",  "theme": "Industrial", "conviction": "lower", "target_weight": 0.08, "thesis": "Electrification."},
    ],
    "cash_target": 0.0,
}

# (label, start, end, character)
WINDOWS = [
    ("COVID crash + recovery", "2020-01-02", "2022-01-03", "crash->bull"),
    ("Into the 2022 bear",     "2021-06-01", "2023-06-01", "peak->bear->recover"),
    ("2022 bear-heavy",        "2022-01-03", "2024-01-02", "bear->recover"),
    ("Recovery bull",          "2023-01-03", "2025-01-02", "bull"),
    ("Recent (our window)",    "2024-06-03", "2026-06-17", "bull"),
]


def _framework_return():
    with ledger.connect() as conn:
        initial = float(ledger.get_meta(conn, "initial_deposit"))
        row = conn.execute(
            "SELECT total_value FROM equity ORDER BY date DESC LIMIT 1").fetchone()
        trades = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE rule != 'Seed'").fetchone()["c"]
    return row["total_value"] / initial - 1, trades


def _buy_hold_and_spx(tickers, weights, start, end):
    df = yf.download(tickers + ["^GSPC"], start=start, end=end, auto_adjust=True,
                     progress=False, group_by="ticker")
    closes = {}
    for t in tickers + ["^GSPC"]:
        try:
            closes[t] = df[t]["Close"].dropna()
        except Exception:
            pass
    wsum = sum(weights[t] for t in tickers if t in closes)
    bh = sum((weights[t] / wsum) * (closes[t].iloc[-1] / closes[t].iloc[0] - 1)
             for t in tickers if t in closes)
    spx = closes["^GSPC"].iloc[-1] / closes["^GSPC"].iloc[0] - 1
    return bh, spx


def main():
    framework = json.loads((CONFIG_DIR / "framework.json").read_text())
    tickers = [p["ticker"] for p in REGIME_BOOK["positions"]]
    weights = {p["ticker"]: p["target_weight"] for p in REGIME_BOOK["positions"]}

    print(f"Regime test — v2 framework ({framework.get('ruleset')}) vs buy & hold "
          "vs S&P, fixed 2-year windows.\n")
    print(f"{'Window':26} {'Character':20} {'v2 fwk':>9} {'trades':>7} "
          f"{'buy&hold':>9} {'S&P':>8} {'fwk vs B&H':>11}")
    print("-" * 96)
    rows = []
    for label, start, end, character in WINDOWS:
        runner.cmd_init(start=start, book=REGIME_BOOK, framework=framework, verbose=False)
        runner.cmd_backtest(start=start, end=end, framework=framework, verbose=False)
        fwk, trades = _framework_return()
        bh, spx = _buy_hold_and_spx(tickers, weights, start, end)
        rows.append((label, character, fwk, trades, bh, spx))
        print(f"{label:26} {character:20} {fwk*100:+8.1f}% {trades:7d} "
              f"{bh*100:+8.1f}% {spx*100:+7.1f}% {(fwk-bh)*100:+10.1f}")

    print("-" * 96)
    print("\nReading: 'fwk vs B&H' > 0 means the rules ADDED value over doing nothing")
    print("(expected in bears, where the thesis-break exit can dodge damage);")
    print("< 0 means the rules COST value (expected in bulls).")

    # Restore the canonical dashboard run (honest 2024 book, v2).
    print("\nRestoring canonical dashboard run...")
    runner.cmd_init(start="2024-06-01", verbose=False)
    runner.cmd_backtest(verbose=False)
    export.export_state()


if __name__ == "__main__":
    main()
