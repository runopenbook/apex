"""The Eleven Rules (Thesis Section 04), as proposal generators.

A rule looks at the day's context and returns zero or more Proposals. The engine
executes proposals (respecting sizing caps, cash, and funding order). Rules never
mutate state themselves — they only decide.

Evaluation order per the thesis, with one practical refinement: the Rule 3 hard
stop is checked before the Rule 1 dip-buy for the same name, so a name that is
−20% on confirmed bad news is never dip-bought.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import judge


@dataclass
class Proposal:
    action: str                       # ADD | TRIM | BUY | SELL | RAISE_CASH | HOLD
    rule: str                         # e.g. "Rule 1"
    rationale: str
    ticker: Optional[str] = None
    target_value: Optional[float] = None   # desired post-trade $ value of the name
    sell_all: bool = False
    judge: str = "mechanical"
    meta: dict = field(default_factory=dict)


@dataclass
class PositionView:
    ticker: str
    theme: str
    conviction: str
    shares: float
    avg_entry: float
    price: float
    day_change: float                 # close-to-prev-close fraction (e.g. -0.06)
    below_ma: bool                    # price below trailing average (downtrend hint)
    flags: dict

    @property
    def value(self) -> float:
        return self.shares * self.price

    @property
    def drawdown(self) -> float:
        if self.avg_entry <= 0:
            return 0.0
        return max(0.0, 1 - self.price / self.avg_entry)


@dataclass
class MarketContext:
    date: str
    total_value: float
    cash: float
    gold_spot: Optional[float]
    benchmark: Optional[float]
    benchmark_high: Optional[float]   # running high-water mark
    mode: str                         # heuristic | claude
    R: dict                           # rules thresholds from framework.json
    sizing: dict
    events: dict = field(default_factory=dict)  # optional injected catalysts

    def w(self, value: float) -> float:
        return value / self.total_value if self.total_value else 0.0


# --- portfolio-level rules ------------------------------------------------

def rule_9_bear_raise_cash(ctx: MarketContext, positions) -> list[Proposal]:
    """Index −10% from highs → raise cash to 20% (cut speculative first)."""
    if ctx.benchmark is None or ctx.benchmark_high is None:
        return []
    idx_dd = 1 - ctx.benchmark / ctx.benchmark_high
    if idx_dd < ctx.R["bear_index_drawdown"]:
        return []
    target_cash = ctx.R["bear_raise_cash_to"] * ctx.total_value
    if ctx.cash >= target_cash:
        return []
    need = target_cash - ctx.cash
    props = []
    # Priority to cut: catalyst/space first, then lower→medium→high conviction.
    rank = {"lower": 0, "medium": 1, "high": 2}
    ordered = sorted(
        positions,
        key=lambda p: (0 if p.flags.get("catalyst_gated") or p.theme == "Space" else 1,
                       rank.get(p.conviction, 1)),
    )
    for p in ordered:
        if need <= 0:
            break
        cut = min(p.value, need)
        if cut < 1:
            continue
        props.append(Proposal(
            "TRIM", "Rule 9",
            f"Confirmed bear (index −{idx_dd*100:.0f}% from highs): raising cash "
            f"toward 20%, trimming {p.ticker} first.",
            ticker=p.ticker, target_value=p.value - cut, judge="mechanical",
        ))
        need -= cut
    return props


def rule_4_5_gold(ctx: MarketContext, gold_pos: Optional[PositionView]) -> list[Proposal]:
    if ctx.gold_spot is None or gold_pos is None:
        return []
    if ctx.gold_spot > ctx.R["gold_add_above"]:
        target = ctx.R["gold_add_to"] * ctx.total_value
        if target > gold_pos.value * 1.01:
            return [Proposal("ADD", "Rule 4",
                    f"Gold spot ${ctx.gold_spot:,.0f} > "
                    f"${ctx.R['gold_add_above']:,}: safe-haven demand — add hedge "
                    f"to {ctx.R['gold_add_to']*100:.0f}%.",
                    ticker=gold_pos.ticker, target_value=target)]
    elif ctx.gold_spot < ctx.R["gold_trim_below"]:
        target = ctx.R["gold_trim_to"] * ctx.total_value
        if target < gold_pos.value * 0.99:
            return [Proposal("TRIM", "Rule 5",
                    f"Gold spot ${ctx.gold_spot:,.0f} < "
                    f"${ctx.R['gold_trim_below']:,}: risk-on — trim hedge to "
                    f"{ctx.R['gold_trim_to']*100:.0f}%, redeploy to growth.",
                    ticker=gold_pos.ticker, target_value=target)]
    return []


# --- per-position rules ---------------------------------------------------

def rule_3_hard_stop(ctx: MarketContext, p: PositionView) -> Optional[Proposal]:
    if p.drawdown < ctx.R["hard_stop_drawdown"]:
        return None
    v = judge.assess_drop(p.ticker, ctx.date, p.day_change, p.drawdown,
                          below_ma=p.below_ma, mode=ctx.mode, kind="stop")
    if v.verdict == "CONFIRMED_BAD":
        return Proposal("SELL", "Rule 3", v.rationale, ticker=p.ticker,
                        sell_all=True, judge=v.judge)
    return Proposal("HOLD", "Rule 3",
                    f"{p.ticker} −{p.drawdown*100:.0f}% from entry but bad news not "
                    f"confirmed — holding. {v.rationale}",
                    ticker=p.ticker, judge=v.judge)


def rule_1_2_dip(ctx: MarketContext, p: PositionView, news) -> Optional[Proposal]:
    if p.day_change > -ctx.R["dip_buy_pct"]:
        return None
    v = judge.assess_drop(p.ticker, ctx.date, p.day_change, p.drawdown,
                          news=news, mode=ctx.mode, kind="dip")
    cap = ctx.sizing["max_single_stock"] * ctx.total_value
    if v.verdict == "NO_NEWS":
        target = min(p.value + ctx.R["dip_buy_add_pp"] * ctx.total_value, cap)
        if target <= p.value + 1:
            return Proposal("HOLD", "Rule 1",
                            f"{p.ticker} dipped on no news but already at the 14% "
                            "cap — holding.", ticker=p.ticker, judge=v.judge)
        return Proposal("ADD", "Rule 1", v.rationale, ticker=p.ticker,
                        target_value=target, judge=v.judge)
    if v.verdict == "NEWS_BROKEN":
        return Proposal("SELL", "Rule 2",
                        f"{p.ticker} −{abs(p.day_change)*100:.1f}% on thesis-breaking "
                        f"news — exit. {v.rationale}",
                        ticker=p.ticker, sell_all=True, judge=v.judge)
    # NEWS_INTACT
    return Proposal("HOLD", "Rule 2",
                    f"{p.ticker} −{abs(p.day_change)*100:.1f}% on news; thesis intact "
                    f"— hold, do not add. {v.rationale}",
                    ticker=p.ticker, judge=v.judge)


def rule_8_contract(ctx: MarketContext, p: PositionView) -> Optional[Proposal]:
    ev = ctx.events.get(p.ticker, {})
    if ev.get("contract_win"):
        cap = ctx.sizing["max_single_stock"] * ctx.total_value
        target = min(p.value + ctx.R["contract_add_pp"] * ctx.total_value, cap)
        return Proposal("ADD", "Rule 8",
                        f"Material contract win for {p.ticker}: {ev['contract_win']}. "
                        "Adding on improved revenue visibility.",
                        ticker=p.ticker, target_value=target, judge="claude")
    return None


def rule_6_7_catalyst(ctx: MarketContext, p: PositionView) -> Optional[Proposal]:
    ev = ctx.events.get(p.ticker, {})
    if ev.get("milestone"):
        target = ctx.R["catalyst_add_to"] * ctx.total_value
        return Proposal("ADD", "Rule 6",
                        f"Confirmed positive milestone for {p.ticker}: "
                        f"{ev['milestone']}. Scaling up the event-gated position.",
                        ticker=p.ticker, target_value=target, judge="claude")
    if ev.get("delay"):
        target = ctx.R["catalyst_cut_to"] * ctx.total_value
        if target < p.value:
            return Proposal("TRIM", "Rule 7",
                            f"Delay/failure for {p.ticker}: {ev['delay']}. Cutting to "
                            "a holding weight (thesis weakened, not broken).",
                            ticker=p.ticker, target_value=target, judge="claude")
    return None


# --- v2: patience-first rule set ------------------------------------------

def rule_thesis_broken(ctx: MarketContext, p: PositionView, news=None) -> Optional[Proposal]:
    """v2 exit: only when a thesis is *confirmed broken*. The price gate is set
    very high (thesis_break_drawdown) so normal volatility never trips it; the
    actual call is a news judgment. Default is HOLD — patience."""
    thr = ctx.R.get("thesis_break_drawdown", 0.50)
    if p.drawdown < thr:
        return None
    v = judge.assess_drop(p.ticker, ctx.date, p.day_change, p.drawdown, news=news,
                          below_ma=p.below_ma, mode=ctx.mode, kind="stop")
    if v.verdict == "CONFIRMED_BAD":
        return Proposal("SELL", "Thesis Broken",
                        f"{p.ticker} −{p.drawdown*100:.0f}% from entry on confirmed "
                        f"structural deterioration — exit. {v.rationale}",
                        ticker=p.ticker, sell_all=True, judge=v.judge)
    return Proposal("HOLD", "Thesis Broken",
                    f"{p.ticker} deeply down but the thesis is not confirmed broken "
                    f"— holding (patience is the default). {v.rationale}",
                    ticker=p.ticker, judge=v.judge)


def rule_add_to_strength(ctx: MarketContext, p: PositionView) -> Optional[Proposal]:
    """v2 add: feed winners on confirmed good news (contract win or milestone).
    The only buy in v2 besides the quarterly review — let winners run *and* add."""
    ev = ctx.events.get(p.ticker, {})
    cap = ctx.sizing["soft_cap_single"] * ctx.total_value
    if ev.get("contract_win"):
        target = min(p.value + ctx.R["contract_add_pp"] * ctx.total_value, cap)
        if target > p.value + 1:
            return Proposal("ADD", "Add to Strength",
                            f"Major contract win for {p.ticker}: {ev['contract_win']}. "
                            "Adding on improved revenue visibility.",
                            ticker=p.ticker, target_value=target, judge="claude")
    if ev.get("milestone"):
        target = min(ctx.R["catalyst_add_to"] * ctx.total_value, cap)
        if target > p.value + 1:
            return Proposal("ADD", "Add to Strength",
                            f"Confirmed milestone for {p.ticker}: {ev['milestone']}. "
                            "Scaling up into demonstrated strength.",
                            ticker=p.ticker, target_value=target, judge="claude")
    return None


def rule_12_soft_cap(ctx: MarketContext, p: PositionView) -> Optional[Proposal]:
    """Rebalance extension: a winner that runs past the soft cap is trimmed back
    to it (not all the way to the 14% hard cap), banking dry powder. Lets winners
    run while preventing any single name from dominating the book."""
    cap = ctx.sizing.get("soft_cap_single", 0.20)
    w = ctx.w(p.value)
    if w > cap + 0.005:
        return Proposal("TRIM", "Rule 12",
                        f"{p.ticker} grew to {w*100:.0f}% of the book — soft-cap "
                        f"rebalance, trimming back to {cap*100:.0f}% and banking the "
                        "proceeds as dip-buy dry powder.",
                        ticker=p.ticker, target_value=cap * ctx.total_value,
                        judge="mechanical")
    return None


def rule_10_maturity(ctx: MarketContext, p: PositionView) -> Optional[Proposal]:
    ev = ctx.events.get(p.ticker, {})
    if ev.get("thesis_matured"):
        return Proposal("SELL", "Rule 10",
                        f"Thesis matured for {p.ticker}: {ev['thesis_matured']}. "
                        "Entry rationale resolved on success — rotating capital out.",
                        ticker=p.ticker, sell_all=True, judge="claude")
    return None
