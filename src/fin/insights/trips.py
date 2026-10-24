"""Trip builder: which transactions belong to a trip.

During the trip: every expense in the date range, except recurring payments (internet, gym,
subscriptions...) and home-life categories, which are listed but not ticked.
Before the trip: Travel-category purchases (flights, hotels, car rental...) made in the months
before, which is when they're usually paid; ticked unless already part of another trip.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd

from . import recurring

HOME_LIFE = ("Housing", "Subscriptions", "Insurance", "Education")
PREBOOK_LOOKBACK_DAYS = 180


def recurring_pairs(con: sqlite3.Connection, df: pd.DataFrame) -> set[tuple[str, str]]:
    return {(s.merchant, s.account) for s in recurring.apply_overrides(con, recurring.detect(df))}


def candidates(df: pd.DataFrame, start: date, end: date, *, accounts: tuple[int, ...] = (),
               recurring_set: set[tuple[str, str]] = frozenset(), tag: str | None = None,
               lookback_days: int = PREBOOK_LOOKBACK_DAYS) -> pd.DataFrame:
    """df: metrics.transactions() frame (amount set). Returns rows with `when` and `include`."""
    exp = df[(df.flow == "expense") & ~df.one_off]
    if accounts:
        exp = exp[exp.account_id.isin(accounts)]
    d = exp.date.dt.date

    during = exp[(d >= start) & (d <= end)].copy()
    during["when"] = "during"
    is_rec = [(m, a) in recurring_set for m, a in zip(during.merchant, during.account)]
    during["include"] = ~pd.Series(is_rec, index=during.index) & ~during.top.isin(HOME_LIFE)
    during["why_not"] = ""
    during.loc[pd.Series(is_rec, index=during.index), "why_not"] = "recurring"
    during.loc[during.top.isin(HOME_LIFE), "why_not"] = "home / fixed cost"

    before = exp[(d >= start - timedelta(days=lookback_days)) & (d < start) & (exp.top == "Travel")].copy()
    before["when"] = "booked before"
    other_trip = before.tags.apply(lambda s: any(t and t not in ("one-off", tag) for t in s.split(", ")))
    before["include"] = ~other_trip
    before["why_not"] = other_trip.map({True: "tagged to another trip", False: ""})

    out = pd.concat([before, during]).sort_values("date")
    return out


def tag_ids(con: sqlite3.Connection, tag: str, tx_ids: list[int]) -> int:
    con.execute("INSERT OR IGNORE INTO tags (name, kind) VALUES (?, 'trip')", (tag,))
    tid = con.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()[0]
    n = 0
    for i in tx_ids:
        n += con.execute("INSERT OR IGNORE INTO tx_tags (tx_id, tag_id, source) VALUES (?, ?, 'manual')",
                         (int(i), tid)).rowcount
    con.commit()
    return n


def untag_ids(con: sqlite3.Connection, tag: str, tx_ids: list[int]) -> None:
    con.executemany(
        "DELETE FROM tx_tags WHERE tx_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)",
        [(int(i), tag) for i in tx_ids],
    )
    con.commit()
