"""Market data + news. Free sources only (yfinance).

Prices: bulk daily history for backtest, latest close for live.
News: pulled per-ticker only when a price trigger fires (keeps it cheap).
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf


def download_closes(tickers, start, end=None) -> pd.DataFrame:
    """Daily close matrix (rows = date strings 'YYYY-MM-DD', cols = tickers).

    Auto-adjusted so splits/dividends don't create phantom gaps.
    """
    tickers = list(dict.fromkeys(tickers))  # de-dupe, keep order
    raw = yf.download(
        tickers, start=start, end=end, auto_adjust=True,
        progress=False, group_by="ticker", threads=True,
    )
    out = {}
    for t in tickers:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                s = raw[t]["Close"]
            else:  # single ticker -> flat columns
                s = raw["Close"]
        except (KeyError, TypeError):
            continue
        out[t] = s
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    df = df.dropna(how="all")
    return df


def latest_closes(tickers) -> dict:
    """Most recent close per ticker (for live/forward mode)."""
    df = download_closes(tickers, start=_days_ago(7))
    if df.empty:
        return {}
    last = df.ffill().iloc[-1]
    return {t: float(last[t]) for t in df.columns if pd.notna(last[t])}


def get_news(ticker, limit=8) -> list:
    """Recent headlines for a ticker (live mode). Returns [] if unavailable.

    Used only when a price trigger fires, so this is rarely called.
    """
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    out = []
    for it in items[:limit]:
        content = it.get("content", it)
        title = content.get("title") or it.get("title")
        if not title:
            continue
        out.append({
            "title": title,
            "publisher": (content.get("provider") or {}).get("displayName")
                          or it.get("publisher", ""),
            "url": (content.get("canonicalUrl") or {}).get("url")
                    or it.get("link", ""),
        })
    return out


def _days_ago(n: int) -> str:
    return (pd.Timestamp.now("UTC") - pd.Timedelta(days=n)).strftime("%Y-%m-%d")
