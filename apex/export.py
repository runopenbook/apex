"""Export the ledger to data/state.json for the static dashboard."""
from __future__ import annotations

import json

from . import ledger
from .paths import STATE_JSON


def export_state():
    with ledger.connect() as conn:
        initial = float(ledger.get_meta(conn, "initial_deposit", 0) or 0)
        start_date = ledger.get_meta(conn, "start_date")
        last_date = ledger.get_meta(conn, "last_date")
        mode = ledger.get_meta(conn, "mode", "backtest")
        strategy = ledger.get_meta(conn, "strategy", "")
        last_prices = ledger.get_meta(conn, "last_prices", {}) or {}
        framework = ledger.get_meta(conn, "framework", {})

        eq = conn.execute(
            "SELECT date, total_value, cash, benchmark_close FROM equity ORDER BY date"
        ).fetchall()

        # Benchmark normalized to the same starting dollars (buy & hold).
        bench0 = next((r["benchmark_close"] for r in eq if r["benchmark_close"]), None)
        curve = []
        for r in eq:
            b = (initial * r["benchmark_close"] / bench0) if (bench0 and r["benchmark_close"]) else None
            curve.append({
                "date": r["date"],
                "value": round(r["total_value"], 2),
                "ret": round(r["total_value"] / initial - 1, 4) if initial else 0,
                "benchmark": round(b, 2) if b else None,
                "benchmark_ret": round(r["benchmark_close"] / bench0 - 1, 4) if (bench0 and r["benchmark_close"]) else None,
                "cash": round(r["cash"], 2),
            })

        positions = []
        theme_val = {}
        invested = 0.0
        for p in ledger.open_positions(conn):
            price = last_prices.get(p["ticker"], p["avg_entry"])
            value = p["shares"] * price
            invested += value
            theme_val[p["theme"]] = theme_val.get(p["theme"], 0) + value
            positions.append({
                "ticker": p["ticker"], "theme": p["theme"],
                "conviction": p["conviction"], "shares": round(p["shares"], 3),
                "avg_entry": round(p["avg_entry"], 2), "price": round(price, 2),
                "value": round(value, 2),
                "drawdown": round(max(0, 1 - price / p["avg_entry"]), 4) if p["avg_entry"] else 0,
                "ret": round(price / p["avg_entry"] - 1, 4) if p["avg_entry"] else 0,
                "thesis": p["thesis"],
            })

        cash = ledger.get_cash(conn)
        total = invested + cash
        for p in positions:
            p["weight"] = round(p["value"] / total, 4) if total else 0
        positions.sort(key=lambda x: -x["value"])

        themes = []
        ranges = framework.get("themes", {})
        for name, val in sorted(theme_val.items(), key=lambda kv: -kv[1]):
            rng = ranges.get(name, {})
            themes.append({"theme": name, "weight": round(val / total, 4) if total else 0,
                           "min": rng.get("min"), "max": rng.get("max")})

        decisions = conn.execute(
            """SELECT date, ticker, action, rule, rationale, judge, price
               FROM decisions ORDER BY id DESC"""
        ).fetchall()
        moves = [dict(d) for d in decisions]

        tx_count = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE rule != 'Seed'"
        ).fetchone()["c"]

        # Grouped trade-days (Autopilot-style): each day's closes + opens, with
        # the weight each name moved from/to (weight = trade $ / that day's value).
        from collections import defaultdict, Counter
        totals = {r["date"]: r["total_value"] for r in eq}
        theme_of = {p["ticker"]: p["theme"] for p in ledger.all_positions(conn)}
        txns = conn.execute(
            "SELECT date, ticker, action, amount FROM transactions ORDER BY id"
        ).fetchall()
        tdmap = defaultdict(lambda: {"opened": [], "closed": []})
        for tx in txns:
            tot = totals.get(tx["date"]) or 1
            item = {"ticker": tx["ticker"], "weight": round(abs(tx["amount"]) / tot, 4),
                    "theme": theme_of.get(tx["ticker"], "")}
            (tdmap[tx["date"]]["closed"] if tx["action"] == "SELL"
             else tdmap[tx["date"]]["opened"]).append(item)
        trade_days = []
        for d in sorted(tdmap, reverse=True):
            g = tdmap[d]
            th = [x["theme"] for x in g["opened"] + g["closed"] if x["theme"]]
            trade_days.append({
                "date": d, "opened": g["opened"], "closed": g["closed"],
                "n": len(g["opened"]) + len(g["closed"]),
                "theme": Counter(th).most_common(1)[0][0] if th else "",
            })

    final = curve[-1] if curve else {"ret": 0, "benchmark_ret": 0}
    state = {
        "meta": {
            "initial_deposit": initial, "start_date": start_date,
            "last_date": last_date, "mode": mode,
            "total_value": round(total, 2), "cash": round(cash, 2),
            "total_return": final.get("ret", 0),
            "benchmark_return": final.get("benchmark_ret", 0),
            "alpha": round((final.get("ret", 0) or 0) - (final.get("benchmark_ret", 0) or 0), 4),
            "num_trades": tx_count,
            "benchmark_label": framework.get("instruments", {}).get("benchmark_label", "Benchmark"),
            "strategy": strategy,
            "disclaimer": "Paper-tracked. Not investment advice. No brokerage "
                          "connection — you execute your own trades.",
        },
        "curve": curve,
        "positions": positions,
        "themes": themes,
        "moves": moves,
        "trade_days": trade_days,
    }
    STATE_JSON.write_text(json.dumps(state, indent=2))
    print(f"Exported {STATE_JSON} "
          f"({len(curve)} days, {len(positions)} positions, {len(moves)} decisions).")
    return state
