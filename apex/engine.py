"""Per-day orchestration: build context, evaluate the rules, execute, log.

One public entry point: run_day(). The runners (forward + backtest) call it.
"""
from __future__ import annotations

import json

from . import ledger, rules, data, judge
from .rules import Proposal, PositionView, MarketContext


def _load_framework(conn):
    return ledger.get_meta(conn, "framework")


def _position_views(conn, closes, prev_closes, ma_map) -> list[PositionView]:
    views = []
    for p in ledger.open_positions(conn):
        t = p["ticker"]
        price = closes.get(t)
        if price is None:
            continue
        prev = prev_closes.get(t, price)
        day_change = (price / prev - 1) if prev else 0.0
        ma = ma_map.get(t)
        views.append(PositionView(
            ticker=t, theme=p["theme"], conviction=p["conviction"],
            shares=p["shares"], avg_entry=p["avg_entry"], price=price,
            day_change=day_change,
            below_ma=(ma is not None and price < ma),
            flags=json.loads(p["flags"] or "{}"),
        ))
    return views


def _portfolio_value(views, cash) -> float:
    return cash + sum(v.value for v in views)


def plan_day(conn, date, closes, prev_closes, ma_map, *, events=None,
             news_provider=None) -> list[dict]:
    """Live pre-scan: which holdings hit a news-judgment trigger today and still
    need a verdict from Claude Code. Queues each missing one (with news) and
    returns the list. Makes no changes to the ledger."""
    fw = _load_framework(conn)
    R = fw["rules"]
    ruleset = fw.get("ruleset", "v1")
    views = _position_views(conn, closes, prev_closes, ma_map)
    events = events or {}
    needed = []
    for v in views:
        if events.get(v.ticker, {}).get("thesis_matured"):
            continue  # decided by event config, no news judgment required
        if ruleset == "v2":
            # v2 only asks one question, and only on a deep drawdown: is the
            # thesis confirmed broken? No daily dip judgments.
            if v.drawdown >= R.get("thesis_break_drawdown", 0.50):
                kind = "stop"
            else:
                continue
        elif v.drawdown >= R["hard_stop_drawdown"]:
            kind = "stop"
        elif v.day_change <= -R["dip_buy_pct"]:
            kind = "dip"
        else:
            continue
        if judge.has_verdict(date, v.ticker, kind):
            continue
        news = news_provider(v.ticker) if news_provider else []
        judge.queue(v.ticker, date, v.day_change, v.drawdown, news, kind)
        needed.append({"ticker": v.ticker, "kind": kind,
                       "day_change": round(v.day_change, 4),
                       "drawdown": round(v.drawdown, 4),
                       "headlines": len(news)})
    return needed


def run_day(conn, date, closes, prev_closes, ma_map, *, mode="heuristic",
            events=None, news_provider=None) -> dict:
    """Evaluate and apply one trading day. Returns a summary dict."""
    fw = _load_framework(conn)
    R, sizing, instr = fw["rules"], fw["sizing"], fw["instruments"]
    cash = ledger.get_cash(conn)
    views = _position_views(conn, closes, prev_closes, ma_map)
    total = _portfolio_value(views, cash)

    benchmark = closes.get(instr["benchmark"])
    gold_spot = closes.get(instr["gold_spot"])
    crude = closes.get(instr["crude"])

    high = ledger.get_meta(conn, "benchmark_high", benchmark) or benchmark
    if benchmark is not None:
        high = max(high or benchmark, benchmark)
        ledger.set_meta(conn, "benchmark_high", high)

    ctx = MarketContext(
        date=date, total_value=total, cash=cash, gold_spot=gold_spot,
        benchmark=benchmark, benchmark_high=high, mode=mode, R=R,
        sizing=sizing, events=events or {},
    )

    by_ticker = {v.ticker: v for v in views}
    gold_ticker = instr["gold_holding"]
    gold_pos = by_ticker.get(gold_ticker)

    fully_invested = fw.get("policy", {}).get("fully_invested", False)
    ruleset = fw.get("ruleset", "v1")
    proposals: list[Proposal] = []

    if ruleset == "v2":
        # Patience-first: default HOLD. Three rare, judgment-based actions only.
        for v in views:
            prop = rules.rule_10_maturity(ctx, v)            # matured -> rotate
            if prop is None and v.drawdown >= R.get("thesis_break_drawdown", 0.50):
                news = news_provider(v.ticker) if (news_provider and mode != "heuristic") else None
                prop = rules.rule_thesis_broken(ctx, v, news=news)   # broken -> exit
            if prop is None:
                prop = rules.rule_add_to_strength(ctx, v)    # confirmed strength -> add
            if prop is not None:
                proposals.append(prop)
    else:
        # v1: the original 11 rules.
        # Portfolio-level first. Rule 9 raises a cash buffer, which is incompatible
        # with the no-cash policy, so it is paused when fully_invested is set.
        if not fully_invested:
            proposals += rules.rule_9_bear_raise_cash(ctx, views)
        proposals += rules.rule_4_5_gold(ctx, gold_pos)

        cut_by_9 = {p.ticker for p in proposals if p.rule == "Rule 9"}
        gold_handled = any(p.ticker == gold_ticker for p in proposals)

        # Per-position, priority order.
        for v in views:
            if v.ticker in cut_by_9:
                continue
            if v.ticker == gold_ticker and gold_handled:
                continue

            prop = rules.rule_10_maturity(ctx, v)
            if prop is None and v.drawdown >= R["hard_stop_drawdown"]:
                prop = rules.rule_3_hard_stop(ctx, v)
            elif prop is None and v.day_change <= -R["dip_buy_pct"]:
                news = news_provider(v.ticker) if (news_provider and mode != "heuristic") else None
                prop = rules.rule_1_2_dip(ctx, v, news)
            if prop is None:
                prop = (rules.rule_8_contract(ctx, v) or rules.rule_6_7_catalyst(ctx, v)
                        or rules.rule_12_soft_cap(ctx, v))
            if prop is not None:
                proposals.append(prop)

    # Execute: sells/trims first (raise cash), then adds.
    actionable = [p for p in proposals if p.action in ("SELL", "TRIM", "ADD")]
    sells = [p for p in actionable if p.action in ("SELL", "TRIM")]
    adds = [p for p in actionable if p.action == "ADD"]

    moves = 0
    for p in sells:
        moves += _execute(conn, date, p, closes)
    # Recompute cash/value after sells for accurate add funding.
    for p in adds:
        moves += _execute(conn, date, p, closes)

    # No-cash policy: sweep any freed cash back into the book.
    if fully_invested:
        swept = sweep_cash(conn, date, closes, sizing.get("soft_cap_single", 0.20),
                           total)
        if swept > 1:
            moves += 1

    # Log HOLDs that carried a rule rationale (informative, not a trade).
    for p in proposals:
        if p.action == "HOLD":
            ledger.log_decision(conn, date, "HOLD", p.rationale, ticker=p.ticker,
                                rule=p.rule, judge=p.judge,
                                price=closes.get(p.ticker))

    # Final equity snapshot for the day.
    cash = ledger.get_cash(conn)
    views = _position_views(conn, closes, prev_closes, ma_map)
    total = _portfolio_value(views, cash)
    ledger.snapshot_equity(conn, date, total, cash, benchmark=benchmark,
                           gold=gold_spot, crude=crude)

    if moves == 0 and not any(p.action == "HOLD" for p in proposals):
        ledger.log_decision(conn, date, "NO_MOVE",
                            "No trigger met today — patience is the default state "
                            "(Rule 11).", rule="Rule 11", judge="mechanical")

    return {"date": date, "moves": moves, "total_value": round(total, 2),
            "cash": round(cash, 2)}


# --- no-cash policy -------------------------------------------------------

def sweep_cash(conn, date, price_map, soft_cap, total_value, *, log=True) -> float:
    """Redeploy idle cash into open positions to keep the book ~100% invested.

    Distributes proportional to target weight, never pushing a name past the soft
    cap, iterating so cap overflow spills to the others. Returns dollars swept.
    """
    start_cash = ledger.get_cash(conn)
    if start_cash < 1:
        return 0.0
    for _ in range(12):
        cash = ledger.get_cash(conn)
        if cash < 1:
            break
        eligible = []
        for p in ledger.open_positions(conn):
            price = price_map.get(p["ticker"])
            if not price or p["target_weight"] <= 0:
                continue
            value = p["shares"] * price
            if value >= soft_cap * total_value - 1:
                continue
            eligible.append((p, price, value))
        if not eligible:
            break
        wsum = sum(p["target_weight"] for p, _, _ in eligible)
        deployed = 0.0
        for p, price, value in eligible:
            room = soft_cap * total_value - value
            want = cash * p["target_weight"] / wsum
            spend = min(want, room, ledger.get_cash(conn))
            if spend < 0.5:
                continue
            buy = spend / price
            new_sh = p["shares"] + buy
            new_avg = (p["shares"] * p["avg_entry"] + spend) / new_sh
            ledger.set_cash(conn, ledger.get_cash(conn) - spend)
            ledger.set_shares(conn, p["ticker"], new_sh, new_avg)
            ledger.log_transaction(conn, date, p["ticker"], "BUY", buy, price,
                                   -spend, "Cash Sweep",
                                   "No-cash policy: redeployed freed cash.")
            deployed += spend
        if deployed < 1:
            break
    swept = start_cash - ledger.get_cash(conn)
    if log and swept > 1:
        ledger.log_decision(conn, date, "ADD",
                            f"No-cash policy: swept ${swept:,.0f} of freed cash back "
                            "across the book to stay fully invested.",
                            rule="Cash Sweep", judge="mechanical")
    return swept


# --- execution primitives -------------------------------------------------

def _execute(conn, date, p: Proposal, closes) -> int:
    pos = ledger.get_position(conn, p.ticker)
    if pos is None:
        return 0
    price = closes.get(p.ticker)
    if not price or price <= 0:
        return 0
    shares, avg_entry = pos["shares"], pos["avg_entry"]
    cash = ledger.get_cash(conn)

    if p.action == "SELL" or (p.action == "TRIM" and p.sell_all):
        if shares <= 0:
            return 0
        proceeds = shares * price
        ledger.set_cash(conn, cash + proceeds)
        ledger.set_shares(conn, p.ticker, 0.0, avg_entry)
        ledger.log_transaction(conn, date, p.ticker, "SELL", shares, price,
                               proceeds, p.rule, p.rationale)
        ledger.log_decision(conn, date, "SELL", p.rationale, ticker=p.ticker,
                            rule=p.rule, judge=p.judge, price=price,
                            meta={"shares": round(shares, 4)})
        return 1

    cur_value = shares * price
    target = p.target_value if p.target_value is not None else cur_value
    delta_value = target - cur_value

    if p.action == "TRIM" or delta_value < 0:
        sell_value = min(cur_value, -delta_value)
        sell_shares = sell_value / price
        if sell_shares < 1e-6:
            return 0
        ledger.set_cash(conn, cash + sell_value)
        ledger.set_shares(conn, p.ticker, shares - sell_shares, avg_entry)
        ledger.log_transaction(conn, date, p.ticker, "SELL", sell_shares, price,
                               sell_value, p.rule, p.rationale)
        ledger.log_decision(conn, date, "TRIM", p.rationale, ticker=p.ticker,
                            rule=p.rule, judge=p.judge, price=price)
        return 1

    if p.action == "ADD" and delta_value > 0:
        need = delta_value
        if cash < need:
            _raise_cash_by_trimming(conn, date, need - cash, exclude=p.ticker,
                                    closes=closes)
            cash = ledger.get_cash(conn)
        spend = min(need, cash)
        if spend < 1:
            return 0
        buy_shares = spend / price
        new_shares = shares + buy_shares
        new_avg = ((shares * avg_entry) + spend) / new_shares if new_shares else price
        ledger.set_cash(conn, cash - spend)
        ledger.set_shares(conn, p.ticker, new_shares, new_avg)
        ledger.log_transaction(conn, date, p.ticker, "BUY", buy_shares, price,
                               -spend, p.rule, p.rationale)
        ledger.log_decision(conn, date, "ADD", p.rationale, ticker=p.ticker,
                            rule=p.rule, judge=p.judge, price=price,
                            meta={"spend": round(spend, 2)})
        return 1
    return 0


def _raise_cash_by_trimming(conn, date, amount, exclude, closes):
    """Fund a buy by trimming the weakest open positions (Rule 1 funding order)."""
    rank = {"lower": 0, "medium": 1, "high": 2}
    candidates = [p for p in ledger.open_positions(conn) if p["ticker"] != exclude]
    candidates.sort(key=lambda p: rank.get(p["conviction"], 1))
    need = amount
    for p in candidates:
        if need <= 0:
            break
        price = closes.get(p["ticker"])
        if not price:
            continue
        value = p["shares"] * price
        take = min(value, need)
        if take < 1:
            continue
        sell_shares = take / price
        cash = ledger.get_cash(conn)
        ledger.set_cash(conn, cash + take)
        ledger.set_shares(conn, p["ticker"], p["shares"] - sell_shares, p["avg_entry"])
        ledger.log_transaction(conn, date, p["ticker"], "SELL", sell_shares, price,
                               take, "Rule 1 funding",
                               f"Trimmed weakest position to fund a higher-conviction add.")
        need -= take
