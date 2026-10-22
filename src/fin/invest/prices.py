"""Daily closes from yfinance, cached in the `prices` table. Only ticker symbols leave the machine."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

import pandas as pd

log = logging.getLogger(__name__)


def _fetch(ticker: str, start: date) -> pd.Series:
    import yfinance as yf

    # auto_adjust=False: VWCE is accumulating (no dividends), and raw closes match broker statements
    h = yf.Ticker(ticker).history(start=start.isoformat(), auto_adjust=False)
    if h.empty:
        return pd.Series(dtype=float)
    s = h["Close"].dropna()
    s.index = s.index.tz_localize(None).date if s.index.tz is not None else s.index.date
    return s


def refresh(con: sqlite3.Connection, fetch=_fetch) -> dict[str, int]:
    """Fetch missing closes for every instrument held at any point. Incremental: re-fetches the
    last few cached days (late corrections) onwards."""
    out = {}
    for r in con.execute(
        """SELECT i.ticker, i.currency, min(l.trade_date) first FROM instruments i JOIN lots l ON l.isin = i.isin
           WHERE i.ticker IS NOT NULL GROUP BY i.ticker, i.currency"""
    ).fetchall():
        tickers = [(r["ticker"], r["currency"], r["first"])]
        if r["currency"] != "EUR":
            tickers.append((f"EUR{r['currency']}=X", r["currency"], r["first"]))
        for ticker, ccy, first in tickers:
            (last,) = con.execute("SELECT max(date) FROM prices WHERE ticker = ?", (ticker,)).fetchone()
            start = date.fromisoformat(last) - timedelta(days=5) if last else date.fromisoformat(first) - timedelta(days=7)
            try:
                s = fetch(ticker, start)
            except Exception as e:  # noqa: BLE001 - a price outage must not break the refresh
                log.warning("price fetch failed for %s: %s", ticker, e)
                continue
            con.executemany(
                "INSERT INTO prices (ticker, date, close, currency) VALUES (?,?,?,?) "
                "ON CONFLICT(ticker, date) DO UPDATE SET close = excluded.close",
                [(ticker, d.isoformat(), float(v), ccy) for d, v in s.items()],
            )
            out[ticker] = len(s)
    con.commit()
    return out


def close_frame(con: sqlite3.Connection, tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Daily (calendar) closes, forward-filled over weekends/holidays."""
    idx = pd.date_range(start, end, freq="D")
    if not tickers:
        return pd.DataFrame(index=idx)
    q = f"SELECT ticker, date, close FROM prices WHERE ticker IN ({','.join('?' * len(tickers))})"
    df = pd.read_sql_query(q, con, params=tickers)
    if df.empty:
        return pd.DataFrame(index=idx, columns=tickers, dtype=float)
    wide = df.pivot(index="date", columns="ticker", values="close")
    wide.index = pd.to_datetime(wide.index)
    return wide.reindex(wide.index.union(idx)).sort_index().ffill().reindex(idx)
