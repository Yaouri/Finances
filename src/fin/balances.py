"""Daily balance per account, reconstructed from balance snapshots + transactions.

From the latest snapshot we walk backwards (subtracting transactions) only as far as history is
continuous: back to the earliest snapshot that still reconciles, then to the first transaction
after the last gap. Accounts with only snapshots (DEGIRO cash) are forward-filled.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd

from .quality import BOOKED


def _snapshots(con: sqlite3.Connection, account_id: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT as_of, balance_type, amount_cents FROM balances WHERE account_id = ? ORDER BY as_of", con,
        params=(account_id,),
    )
    booked = df[df.balance_type.isin(BOOKED)]
    use = booked if not booked.empty else df  # Revolut only reports ITAV
    return use.groupby("as_of", as_index=False).amount_cents.max()


def account_series(con: sqlite3.Connection, account_id: int, end: date | None = None) -> pd.Series:
    end = end or date.today()
    snaps = _snapshots(con, account_id)
    tx = pd.read_sql_query(
        "SELECT booking_date, sum(amount_cents) c FROM transactions WHERE account_id = ? AND duplicate_of IS NULL "
        "GROUP BY booking_date ORDER BY booking_date", con, params=(account_id,),
    )
    if snaps.empty:
        return pd.Series(dtype=float)
    daily_tx = pd.Series(tx.c.values, index=pd.to_datetime(tx.booking_date)) if not tx.empty else pd.Series(dtype=float)

    if daily_tx.empty:  # snapshot-only account
        s = pd.Series(snaps.amount_cents.values, index=pd.to_datetime(snaps.as_of))
        idx = pd.date_range(s.index.min(), end, freq="D")
        return s.reindex(idx).ffill() / 100

    # find the continuous chain ending at the latest snapshot
    chain_start = pd.Timestamp(snaps.as_of.iloc[-1])
    gap_left = None
    for i in range(len(snaps) - 1, 0, -1):
        a, b = snaps.iloc[i - 1], snaps.iloc[i]
        moved = daily_tx[(daily_tx.index > a.as_of) & (daily_tx.index <= b.as_of)].sum()
        if b.amount_cents - a.amount_cents - moved != 0:
            gap_left = pd.Timestamp(a.as_of)
            break
        chain_start = pd.Timestamp(a.as_of)
    first_tx = daily_tx.index.min() if gap_left is None else daily_tx[daily_tx.index > gap_left].index.min()
    start = min(first_tx, chain_start) if pd.notna(first_tx) else chain_start
    start = start - pd.Timedelta(days=1)  # show the opening balance

    idx = pd.date_range(start, end, freq="D")
    flows = daily_tx.reindex(idx).fillna(0)
    anchor_day, anchor = pd.Timestamp(snaps.as_of.iloc[-1]), snaps.amount_cents.iloc[-1]
    cum = flows.cumsum()
    # balance(d) = anchor + (cum(d) - cum(anchor_day))
    bal = anchor + cum - (cum.loc[anchor_day] if anchor_day in cum.index else cum.iloc[-1])
    return bal / 100


def all_accounts(con: sqlite3.Connection, end: date | None = None) -> pd.DataFrame:
    """Columns = account ids, index = day, values = EUR (full balances, not joint share)."""
    cols = {}
    for (aid,) in con.execute("SELECT id FROM accounts WHERE active = 1"):
        s = account_series(con, aid, end)
        if not s.empty:
            cols[aid] = s
    return pd.DataFrame(cols).sort_index()
