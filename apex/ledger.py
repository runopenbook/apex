"""SQLite ledger: holdings, cash, transactions, decision log, daily equity.

Everything the portfolio knows about itself lives here. Plain SQL, inspectable
with any sqlite viewer.
"""
from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from typing import Optional

from .paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    ticker        TEXT PRIMARY KEY,
    theme         TEXT NOT NULL,
    conviction    TEXT NOT NULL,
    shares        REAL NOT NULL DEFAULT 0,
    avg_entry     REAL NOT NULL DEFAULT 0,
    target_weight REAL NOT NULL DEFAULT 0,
    thesis        TEXT,
    flags         TEXT DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'open'   -- open | closed
);

CREATE TABLE IF NOT EXISTS transactions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,
    ticker    TEXT NOT NULL,
    action    TEXT NOT NULL,                     -- BUY | SELL
    shares    REAL NOT NULL,
    price     REAL NOT NULL,
    amount    REAL NOT NULL,                     -- signed cash impact (- buy, + sell)
    rule      TEXT,
    rationale TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,
    ticker    TEXT,                              -- NULL for portfolio-level / no-move days
    action    TEXT NOT NULL,                     -- BUY | SELL | HOLD | NO_MOVE | TRIM | ADD
    rule      TEXT,
    rationale TEXT NOT NULL,
    judge     TEXT NOT NULL DEFAULT 'mechanical',-- mechanical | heuristic | claude | council
    price     REAL,
    meta      TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS equity (
    date            TEXT PRIMARY KEY,
    total_value     REAL NOT NULL,
    cash            REAL NOT NULL,
    benchmark_close REAL,
    gold_close      REAL,
    crude_close     REAL
);
"""


@contextmanager
def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def reset_db(db_path=DB_PATH) -> None:
    if db_path.exists():
        db_path.unlink()
    with connect(db_path) as conn:
        init_db(conn)


# --- meta -----------------------------------------------------------------

def set_meta(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def get_cash(conn) -> float:
    return float(get_meta(conn, "cash", 0.0))


def set_cash(conn, amount: float) -> None:
    set_meta(conn, "cash", round(amount, 2))


# --- positions ------------------------------------------------------------

def upsert_position(conn, ticker, theme, conviction, target_weight,
                    thesis="", flags=None, shares=0.0, avg_entry=0.0) -> None:
    conn.execute(
        """INSERT INTO positions(ticker, theme, conviction, shares, avg_entry,
                                 target_weight, thesis, flags, status)
           VALUES(?,?,?,?,?,?,?,?, 'open')
           ON CONFLICT(ticker) DO UPDATE SET
             theme=excluded.theme, conviction=excluded.conviction,
             target_weight=excluded.target_weight, thesis=excluded.thesis,
             flags=excluded.flags""",
        (ticker, theme, conviction, shares, avg_entry, target_weight,
         thesis, json.dumps(flags or {})),
    )


def get_position(conn, ticker) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()


def open_positions(conn):
    return conn.execute(
        "SELECT * FROM positions WHERE status='open' AND shares > 0"
    ).fetchall()


def all_positions(conn):
    return conn.execute("SELECT * FROM positions").fetchall()


def set_shares(conn, ticker, shares, avg_entry) -> None:
    status = "open" if shares > 1e-9 else "closed"
    conn.execute(
        "UPDATE positions SET shares=?, avg_entry=?, status=? WHERE ticker=?",
        (shares, avg_entry, status, ticker),
    )


def set_target_weight(conn, ticker, weight) -> None:
    conn.execute("UPDATE positions SET target_weight=? WHERE ticker=?",
                 (weight, ticker))


# --- logging --------------------------------------------------------------

def log_transaction(conn, date, ticker, action, shares, price, amount,
                    rule=None, rationale="") -> None:
    conn.execute(
        """INSERT INTO transactions(date, ticker, action, shares, price, amount,
                                    rule, rationale)
           VALUES(?,?,?,?,?,?,?,?)""",
        (date, ticker, action, shares, price, amount, rule, rationale),
    )


def log_decision(conn, date, action, rationale, ticker=None, rule=None,
                 judge="mechanical", price=None, meta=None) -> None:
    conn.execute(
        """INSERT INTO decisions(date, ticker, action, rule, rationale, judge,
                                 price, meta)
           VALUES(?,?,?,?,?,?,?,?)""",
        (date, ticker, action, rule, rationale, judge, price,
         json.dumps(meta or {})),
    )


def snapshot_equity(conn, date, total_value, cash, benchmark=None,
                    gold=None, crude=None) -> None:
    conn.execute(
        """INSERT INTO equity(date, total_value, cash, benchmark_close,
                              gold_close, crude_close)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
             total_value=excluded.total_value, cash=excluded.cash,
             benchmark_close=excluded.benchmark_close,
             gold_close=excluded.gold_close, crude_close=excluded.crude_close""",
        (date, round(total_value, 2), round(cash, 2), benchmark, gold, crude),
    )
