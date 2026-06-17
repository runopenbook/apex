"""Quarterly strategic review — the only place the *theme/allocation* layer changes.

The 11 daily rules are tactical. This is strategic: once per calendar quarter the
AI reconsiders each theme's structural health and may free-re-target the book
(new target weights within each theme's min/max range), add a newly-structural
theme, or drop one whose multi-year thesis has matured/broken.

It is a forward-looking judgment, so it cannot be faithfully backtested (that would
be look-ahead). In backtest the cadence is logged as checkpoints with no change;
in forward mode the engine pauses for Claude Code to write the review, then
rebalances the book to the new targets.

Flow (forward): a new quarter → write data/pending_review.json and stop. Claude
Code reasons per theme and writes data/review.json[<quarter>], then re-run applies
it. Verdict schema:

  { "2026Q3": {
      "targets": { "NVDA": 0.13, "AVGO": 0.12, ... },   # per-ticker new weights
      "add":  [ {"ticker":"X","theme":"...","conviction":"high","target_weight":0.05,"thesis":"..."} ],
      "drop": [ "RKLB" ],
      "rationale": "AI capex mid-cycle, trimmed; added power theme ..." } }
"""
from __future__ import annotations

import json

from . import ledger
from .paths import DATA_DIR

PENDING_REVIEW = DATA_DIR / "pending_review.json"
REVIEW_VERDICTS = DATA_DIR / "review.json"


def quarter_label(date: str) -> str:
    y, m = int(date[:4]), int(date[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def review_due(conn, date: str) -> bool:
    return quarter_label(date) != ledger.get_meta(conn, "last_review_quarter")


def _load(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def load_verdict(quarter: str):
    return _load(REVIEW_VERDICTS).get(quarter)


def _book_snapshot(conn, price_map):
    positions = ledger.open_positions(conn)
    cash = ledger.get_cash(conn)
    total = cash + sum(p["shares"] * price_map.get(p["ticker"], 0) for p in positions)
    return positions, cash, total


def write_request(conn, date, quarter, price_map, framework) -> dict:
    positions, cash, total = _book_snapshot(conn, price_map)
    themes = framework["themes"]
    theme_real = {}
    pos_rows = []
    for p in positions:
        val = p["shares"] * price_map.get(p["ticker"], 0)
        theme_real[p["theme"]] = theme_real.get(p["theme"], 0) + val
        pos_rows.append({
            "ticker": p["ticker"], "theme": p["theme"],
            "conviction": p["conviction"],
            "target_weight": round(p["target_weight"], 4),
            "realized_weight": round(val / total, 4) if total else 0,
            "thesis": p["thesis"],
        })
    req = {
        "quarter": quarter, "date": date,
        "themes": [{"theme": t, "min": r.get("min"), "max": r.get("max"),
                    "realized_weight": round(theme_real.get(t, 0) / total, 4) if total else 0}
                   for t, r in themes.items() if t != "Cash"],
        "positions": pos_rows,
        "instructions": (
            "Reconsider each theme's structural health (is the multi-year demand "
            "visibility still intact and still early/mid-cycle?). Default is NO "
            "CHANGE. Where a criterion trips, free-re-target within each theme's "
            "min/max range; you may add a newly-structural theme/name or drop one "
            "whose thesis has matured or broken. Targets should sum to ~1.00 "
            "(no-cash policy). Provide a rationale."),
        "schema_hint": {
            "targets": {"TICKER": 0.0},
            "add": [{"ticker": "", "theme": "", "conviction": "high|medium|lower",
                     "target_weight": 0.0, "thesis": ""}],
            "drop": ["TICKER"], "rationale": ""},
    }
    PENDING_REVIEW.write_text(json.dumps(req, indent=2))
    return req


def apply_review(conn, date, quarter, verdict, price_map, framework) -> int:
    """Apply a review verdict: roster changes + new targets, then rebalance."""
    n = 0
    # Adds (new positions, 0 shares — the rebalance/sweep will buy them in).
    for a in verdict.get("add", []):
        ledger.upsert_position(conn, a["ticker"], a["theme"], a.get("conviction", "lower"),
                               a.get("target_weight", 0.0), thesis=a.get("thesis", ""),
                               flags=a.get("flags", {}), shares=0.0, avg_entry=0.0)
        ledger.log_decision(conn, date, "ADD",
                            f"Quarterly review {quarter}: added {a['ticker']} "
                            f"({a['theme']}). {a.get('thesis','')}",
                            ticker=a["ticker"], rule="Quarterly Review", judge="review")
        n += 1
    # Drops -> target 0 (rebalance sells to zero).
    for t in verdict.get("drop", []):
        if ledger.get_position(conn, t):
            ledger.set_target_weight(conn, t, 0.0)
            ledger.log_decision(conn, date, "SELL",
                                f"Quarterly review {quarter}: dropping {t} — theme/"
                                "thesis no longer structural.", ticker=t,
                                rule="Quarterly Review", judge="review")
    # New targets.
    for ticker, w in verdict.get("targets", {}).items():
        if ledger.get_position(conn, ticker):
            ledger.set_target_weight(conn, ticker, float(w))

    # Theme-range sanity check (free re-target must still respect the bands).
    _warn_theme_bands(conn, date, quarter, framework)

    n += rebalance_to_targets(conn, date, price_map, rule="Quarterly Review",
                              judge="review")
    if verdict.get("rationale"):
        ledger.log_decision(conn, date, "REVIEW",
                            f"Quarterly review {quarter}: {verdict['rationale']}",
                            rule="Quarterly Review", judge="review")
    PENDING_REVIEW.write_text("{}")
    return n


def _warn_theme_bands(conn, date, quarter, framework):
    themes = framework["themes"]
    by_theme = {}
    for p in ledger.all_positions(conn):
        by_theme[p["theme"]] = by_theme.get(p["theme"], 0) + max(0.0, p["target_weight"])
    for t, w in by_theme.items():
        r = themes.get(t, {})
        lo, hi = r.get("min"), r.get("max")
        if hi is not None and (w > hi + 1e-6 or w < (lo or 0) - 1e-6):
            ledger.log_decision(conn, date, "REVIEW",
                                f"Quarterly review {quarter}: WARNING — {t} target "
                                f"{w*100:.0f}% is outside its {lo*100:.0f}-{hi*100:.0f}% "
                                "band.", rule="Quarterly Review", judge="review")


def rebalance_to_targets(conn, date, price_map, *, rule="Quarterly Review",
                         judge="review") -> int:
    """Trade every position toward target_weight * total. Sells first, then buys."""
    positions = ledger.all_positions(conn)
    cash = ledger.get_cash(conn)
    total = cash + sum(p["shares"] * price_map.get(p["ticker"], 0) for p in positions)
    if total <= 0:
        return 0
    moves = 0
    # Sells / trims.
    for p in positions:
        price = price_map.get(p["ticker"])
        if not price:
            continue
        cur = p["shares"] * price
        tgt = max(0.0, p["target_weight"]) * total
        if tgt < cur - 1:
            sell_shares = (cur - tgt) / price
            ledger.set_cash(conn, ledger.get_cash(conn) + (cur - tgt))
            ledger.set_shares(conn, p["ticker"], p["shares"] - sell_shares, p["avg_entry"])
            ledger.log_transaction(conn, date, p["ticker"], "SELL", sell_shares, price,
                                   cur - tgt, rule, "Rebalance to new quarterly target.")
            moves += 1
    # Buys (largest underweight first).
    positions = ledger.all_positions(conn)
    gaps = []
    for p in positions:
        price = price_map.get(p["ticker"])
        if not price or p["target_weight"] <= 0:
            continue
        cur = p["shares"] * price
        tgt = p["target_weight"] * total
        if tgt > cur + 1:
            gaps.append((tgt - cur, p, price))
    gaps.sort(key=lambda g: -g[0])
    for need, p, price in gaps:
        cash = ledger.get_cash(conn)
        spend = min(need, cash)
        if spend < 1:
            continue
        buy = spend / price
        new_sh = p["shares"] + buy
        new_avg = ((p["shares"] * p["avg_entry"]) + spend) / new_sh if new_sh else price
        ledger.set_cash(conn, cash - spend)
        ledger.set_shares(conn, p["ticker"], new_sh, new_avg)
        ledger.log_transaction(conn, date, p["ticker"], "BUY", buy, price, -spend,
                               rule, "Rebalance to new quarterly target.")
        moves += 1
    return moves
