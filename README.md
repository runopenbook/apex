# Apex Portfolio — AI-managed paper-trading simulator

Brings the **Apex Strategy Thesis** to life: a rule-bound AI agent runs a
concentrated book, every move is logged with the rule that triggered it and a
plain-language rationale, and a dashboard shows the equity curve, holdings, and a
clickable timeline of decisions — like an "autopilot" portfolio view.

> Paper-trading simulation. **Not investment advice.** No real brokerage
> connection. The app never places real trades.

## How it works

- **Engine** (`apex/`): each trading day it pulls prices (holdings + S&P + gold +
  crude), applies the **11 rules in order**, and emits a definitive
  BUY / SELL / HOLD / NO-MOVE with rationale.
- **Price-gated by design:** most rules only fire after a price trigger, so most
  days are NO-MOVE (Rule 11) — handled in free code. The AI is only consulted to
  *judge news* on the rare day a trigger fires. That keeps cost at ~$0.
- **Two modes:**
  - `backtest` — replays history day-by-day. News judgment uses a deterministic
    **heuristic** (badged as such), since reliable per-day historical news isn't
    free. Illustrative, not proof.
  - `forward` — one live day at a time. News-judgment rules surface a request that
    **Claude Code answers** with real headlines + rationale (no API key, no cost).
- **Ledger** (`data/apex.db`, SQLite): holdings, cash, transactions, decision log,
  daily equity. Inspect with any SQLite viewer.

## Quick start

```bash
py -m pip install -r requirements.txt

# init the book + backtest + export dashboard JSON, in one go:
py -m apex.runner run --start 2024-06-01

# view the dashboard:
py serve.py            # opens http://localhost:8765/dashboard/index.html
```

Other commands:

```bash
py -m apex.runner init --start 2024-06-01 --capital 100000
py -m apex.runner backtest
py -m apex.runner forward      # one live day (uses Claude Code for news judgment)
py -m apex.runner export       # rebuild dashboard JSON from the ledger
```

## Configuration

- `config/book.json` — the 11 positions, themes, convictions, target weights.
- `config/framework.json` — theme ranges, sizing caps, and every rule threshold.
- `config/events.json` (optional) — inject catalysts/contracts/maturities for the
  event-gated rules (6, 7, 8, 10), keyed by `{date: {ticker: {...}}}`.

## Live forward judgment (the Claude Code loop)

In `forward` mode, when a judgment rule fires, the engine writes the question to
`data/pending_judgments.json`. Claude Code reads it, looks up real news, and writes
a verdict to `data/judgments.json` (key `date:ticker:kind`):

```json
{ "2026-06-17:NVDA:dip": { "verdict": "NO_NEWS",
  "rationale": "5% drop with no company-specific news — market noise.",
  "confidence": "high" } }
```

Re-running the day picks up the verdict. This is what makes the AGENT *you*, at
zero API cost.

## House changes to the thesis

- **No cash (fully invested).** `policy.fully_invested` in `framework.json` keeps
  the book ~100% invested: any freed cash (from trims, gold rotation, soft-cap
  rebalances) is swept back into positions the same day, proportional to target
  weight and capped at the soft cap. Cash sits at $0.
- **Rule 9 is paused** under the no-cash policy (raising a 20% cash buffer
  contradicts it). Can later be reinterpreted as a de-risking *rotation* instead.
- **Soft-cap rebalance (Rule 12).** A winner past `soft_cap_single` (20%) is
  trimmed back to it — lets winners run without any single name dominating.
- **Quarterly strategic review.** Once per calendar quarter the *theme/allocation*
  layer is reconsidered (separate from the daily tactical rules). Free re-target
  within each theme's min/max band; may add a newly-structural theme/name or drop
  one whose thesis has matured. It's a forward-looking judgment, so backtest only
  logs held checkpoints (no look-ahead); **forward mode pauses for Claude Code** to
  write the review, then rebalances the book to the new targets. Flow:
  `forward` on a new quarter → writes `data/pending_review.json` and stops → Claude
  Code writes `data/review.json["<quarter>"]` (targets / add / drop / rationale) →
  re-run `forward` applies it. Config in `framework.json` → `review`.

## Known limitations (by design, for now)

1. **Theme concentration isn't capped.** Rule 12 caps single names, but a theme
   can still run hot if several names each stay under the single-stock cap
   (e.g. Defense via PLTR+BWXT+LMT).
2. **No re-entry after exits.** Rule 3 stops remove names permanently; there's no
   codified re-entry mechanism yet.
3. **Backtest news judgment is heuristic**, not real news. Forward mode is faithful.
4. **Per-position "return" is on blended cost basis** — the daily cash sweep buys
   more over time, so a name's reported return reflects dollar-cost-averaged entry,
   not raw price appreciation.
