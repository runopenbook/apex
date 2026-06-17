"""The judgment layer — answers the qualitative questions the thesis asks.

Rules 1/2/3 (and the event rules) need a read on *news*, not just price. Three
providers:

  - 'mechanical'  : never called here (pure-threshold rules bypass the judge).
  - 'heuristic'   : deterministic, free. Used in backtest where reliable per-day
                    historical news isn't available. Clearly badged as lower
                    confidence in the decision log.
  - 'claude'      : live mode. Writes a pending request to data/pending_judgments
                    .json for Claude Code (me) to answer with real news + rationale,
                    then reads the answer back from data/judgments.json. No API key,
                    no per-call cost.

Verdict is one of: NO_NEWS, NEWS_INTACT, NEWS_BROKEN, CONFIRMED_BAD, NOT_CONFIRMED.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict

from .paths import PENDING_JUDGMENTS, JUDGMENTS


@dataclass
class Verdict:
    verdict: str          # NO_NEWS | NEWS_INTACT | NEWS_BROKEN | CONFIRMED_BAD | NOT_CONFIRMED
    rationale: str
    judge: str            # heuristic | claude
    confidence: str = "medium"


def assess_drop(ticker, date, change_pct, drawdown, *, news=None,
                below_ma=False, mode="heuristic", kind="dip") -> Verdict:
    """kind: 'dip' (Rules 1/2) or 'stop' (Rule 3)."""
    if mode == "claude":
        v = _claude_lookup(ticker, date, kind)
        if v is not None:
            return v
        _queue_request(ticker, date, change_pct, drawdown, news, kind)
        # Until answered, default to the patient/non-destructive branch.
        if kind == "stop":
            return Verdict("NOT_CONFIRMED",
                           "Awaiting Claude judgment; holding per patience default.",
                           "claude", "low")
        return Verdict("NEWS_INTACT",
                       "Awaiting Claude judgment; no add until confirmed.",
                       "claude", "low")
    return _heuristic(ticker, change_pct, drawdown, news, below_ma, kind)


def _heuristic(ticker, change_pct, drawdown, news, below_ma, kind) -> Verdict:
    has_news = bool(news)
    if kind == "stop":
        # Rule 3 hard stop. Without reliable news, approximate "confirmed bad
        # news" by a deep drawdown that is *still* deteriorating (below trend).
        if below_ma:
            return Verdict("CONFIRMED_BAD",
                           f"{ticker} −{drawdown*100:.0f}% from entry and still "
                           "below trend; treated as a thesis change (heuristic).",
                           "heuristic")
        return Verdict("NOT_CONFIRMED",
                       f"{ticker} deeply down but stabilizing above trend; "
                       "thesis not confirmed broken (heuristic).",
                       "heuristic")
    # dip (Rules 1/2)
    if not has_news:
        return Verdict("NO_NEWS",
                       f"{ticker} dropped {change_pct*100:.1f}% with no material "
                       "news found — treated as noise (Rule 1).",
                       "heuristic")
    # News present but unjudged automatically -> stay patient, don't add.
    return Verdict("NEWS_INTACT",
                   f"{ticker} dropped {change_pct*100:.1f}% on news; thesis "
                   "presumed intact, holding without adding (heuristic).",
                   "heuristic")


# --- claude pending-queue plumbing ---------------------------------------

def has_verdict(date, ticker, kind) -> bool:
    return f"{date}:{ticker}:{kind}" in _load(JUDGMENTS)


def queue(ticker, date, change_pct, drawdown, news, kind):
    """Public wrapper: record a judgment request for Claude Code to answer."""
    _queue_request(ticker, date, change_pct, drawdown, news, kind)


def clear_pending():
    if PENDING_JUDGMENTS.exists():
        PENDING_JUDGMENTS.write_text("{}")


def _load(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _claude_lookup(ticker, date, kind):
    answers = _load(JUDGMENTS)
    key = f"{date}:{ticker}:{kind}"
    a = answers.get(key)
    if not a:
        return None
    return Verdict(a["verdict"], a.get("rationale", ""), "claude",
                   a.get("confidence", "medium"))


def _queue_request(ticker, date, change_pct, drawdown, news, kind):
    pending = _load(PENDING_JUDGMENTS)
    key = f"{date}:{ticker}:{kind}"
    pending[key] = {
        "ticker": ticker, "date": date, "kind": kind,
        "change_pct": round(change_pct, 4),
        "drawdown": round(drawdown, 4),
        "news": news or [],
        "question": ("Does confirmed bad news break the thesis? (CONFIRMED_BAD / "
                     "NOT_CONFIRMED)") if kind == "stop" else
                    ("Is the drop on no-news noise, intact-thesis news, or "
                     "thesis-breaking news? (NO_NEWS / NEWS_INTACT / NEWS_BROKEN)"),
    }
    PENDING_JUDGMENTS.write_text(json.dumps(pending, indent=2))
