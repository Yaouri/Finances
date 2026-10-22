"""Positions, FIFO cost basis, realised/unrealised gains and the daily value series."""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from . import prices


@dataclass
class Position:
    isin: str
    name: str
    ticker: str
    quantity: float = 0.0
    cost_eur: float = 0.0          # FIFO cost of open units, incl. buy fees
    realised_eur: float = 0.0
    price: float | None = None
    price_date: str | None = None
    open_lots: deque = field(default_factory=deque, repr=False)  # [qty, unit_cost]

    @property
    def value_eur(self) -> float | None:
        return None if self.price is None else self.quantity * self.price

    @property
    def unrealised_eur(self) -> float | None:
        return None if self.value_eur is None else self.value_eur - self.cost_eur


def active_lots(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT l.*, i.name, i.currency AS inst_currency FROM lots l JOIN instruments i ON i.isin = l.isin
           WHERE l.superseded = 0 ORDER BY l.trade_date, coalesce(l.trade_time, ''), l.id""",
        con,
    )


def positions(con: sqlite3.Connection) -> dict[str, Position]:
    pos: dict[str, Position] = {}
    for r in active_lots(con).itertuples():
        p = pos.setdefault(r.isin, Position(r.isin, r.name, r.ticker))
        if r.quantity > 0:
            unit = -r.total_eur / r.quantity
            p.open_lots.append([r.quantity, unit])
        else:
            sell, proceeds, cost = -r.quantity, r.total_eur, 0.0
            while sell > 1e-9 and p.open_lots:
                lot = p.open_lots[0]
                take = min(lot[0], sell)
                cost += take * lot[1]
                lot[0] -= take
                sell -= take
                if lot[0] <= 1e-9:
                    p.open_lots.popleft()
            p.realised_eur += proceeds - cost
    for p in pos.values():
        p.quantity = sum(q for q, _ in p.open_lots)
        p.cost_eur = sum(q * u for q, u in p.open_lots)
        row = con.execute("SELECT date, close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
                          (p.ticker,)).fetchone()
        if row and p.quantity:
            p.price, p.price_date = row["close"], row["date"]
    return pos


def summary(con: sqlite3.Connection) -> dict:
    pos = [p for p in positions(con).values()]
    open_ = [p for p in pos if p.quantity > 1e-9]
    value = sum(p.value_eur or 0 for p in open_)
    cost = sum(p.cost_eur for p in open_)
    fees = float(con.execute("SELECT coalesce(sum(fees), 0) FROM lots WHERE superseded = 0").fetchone()[0])
    return {
        "positions": open_, "value_eur": value, "invested_eur": cost, "unrealised_eur": value - cost,
        "unrealised_pct": (value - cost) / cost if cost else 0.0,
        "realised_eur": sum(p.realised_eur for p in pos), "fees_eur": fees,
        "has_transfer_placeholder": bool(con.execute(
            "SELECT 1 FROM lots WHERE kind='transfer_in' AND superseded=0").fetchone()),
    }


def daily_series(con: sqlite3.Connection, end: date | None = None) -> pd.DataFrame:
    """Per day: market value, cumulative net contributions (cash put in minus taken out), units per ticker."""
    lots = active_lots(con)
    if lots.empty:
        return pd.DataFrame(columns=["value_eur", "contributed_eur"])
    end = end or date.today()
    start = date.fromisoformat(lots.trade_date.min())
    idx = pd.date_range(start, end, freq="D")
    lots["d"] = pd.to_datetime(lots.trade_date)
    units = lots.pivot_table(index="d", columns="ticker", values="quantity", aggfunc="sum").reindex(idx).fillna(0).cumsum()
    flows = (-lots.groupby("d").total_eur.sum()).reindex(idx).fillna(0).cumsum()
    closes = prices.close_frame(con, list(units.columns), start, end)
    # non-EUR tickers -> EUR via EURxxx=X
    ccy = dict(con.execute("SELECT ticker, currency FROM instruments WHERE ticker IS NOT NULL").fetchall())
    for t in units.columns:
        if ccy.get(t, "EUR") != "EUR":
            fx = prices.close_frame(con, [f"EUR{ccy[t]}=X"], start, end).iloc[:, 0]
            closes[t] = closes[t] / fx
    value = (units * closes.reindex(columns=units.columns)).sum(axis=1, min_count=1)
    out = pd.DataFrame({"value_eur": value, "contributed_eur": flows})
    return out.join(units.add_prefix("units_"))
