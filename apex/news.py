"""Free news layer for the Content Studio — no API key, no cost.

Reads each book's current holdings and pulls recent headlines from Google News
RSS (per ticker), then writes data/news.json. The Studio displays these and
drafts "news posts" tying a headline to the position it relates to.

Run:  py -m apex.news        (the refresh workflow runs it on a schedule)
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# (state file, display name) — same five books
BOOKS = [
    ("ignition_state.json", "Ignition"),
    ("state.json", "Apex"),
    ("slipstream_state.json", "Slipstream"),
    ("nasdaq34_state.json", "Sweet Spot"),
    ("bedrock_state.json", "Bedrock"),
]

PER_TICKER = 3          # headlines kept per ticker
TOTAL_CAP = 40          # max items in the flat feed
UA = "Mozilla/5.0 (compatible; OpenBookNews/1.0)"


def holdings():
    """ticker -> sorted list of book names that hold it (current positions)."""
    out: dict[str, list[str]] = {}
    for f, name in BOOKS:
        p = DATA / f
        if not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        for pos in s.get("positions", []):
            t = pos.get("ticker")
            if t:
                out.setdefault(t, [])
                if name not in out[t]:
                    out[t].append(name)
    return out


def rss(ticker):
    """Top Google News RSS items for a ticker. Returns [] on any failure."""
    q = urllib.parse.quote(f'"{ticker}" stock')
    url = (f"https://news.google.com/rss/search?q={q}"
           "&hl=en-US&gl=US&ceid=US:en")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        print(f"  ! {ticker}: {e}")
        return []
    items = []
    for it in root.findall(".//item")[:PER_TICKER]:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        # Google prefixes " - Source" on titles; strip it, we show source separately
        if src and title.endswith(" - " + src):
            title = title[: -(len(src) + 3)]
        if title and link:
            items.append({"title": title, "link": link, "source": src, "published": pub})
    return items


def main():
    held = holdings()
    print(f"Fetching news for {len(held)} tickers...")
    by_ticker = {}
    flat = []
    for t, books in held.items():
        items = rss(t)
        if not items:
            continue
        by_ticker[t] = {"books": books, "items": items}
        for it in items:
            flat.append({"ticker": t, "books": books, **it})
        print(f"  {t}: {len(items)}")

    def keyf(it):                       # newest first (RFC-822 dates sort poorly as text)
        try:
            return datetime.strptime(it["published"][:25], "%a, %d %b %Y %H:%M:%S")
        except Exception:
            return datetime(1970, 1, 1)
    flat.sort(key=keyf, reverse=True)

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tickers": by_ticker,
        "items": flat[:TOTAL_CAP],
    }
    (DATA / "news.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote data/news.json — {len(out['items'])} items across {len(by_ticker)} tickers")


if __name__ == "__main__":
    main()
