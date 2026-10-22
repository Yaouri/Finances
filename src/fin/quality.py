"""Data-quality checks: every known balance snapshot vs the running sum of transactions.

A constant offset between consecutive snapshots is fine (history before the first transaction);
a *change* in the offset means transactions are missing or duplicated in that interval.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from itertools import pairwise

# snapshot types that represent booked balances (ITAV etc. include pending/holds)
BOOKED = ("STATEMENT", "CLBD", "MANUAL", "ITBD")


@dataclass
class Gap:
    account: str
    from_date: str
    to_date: str
    delta_cents: int  # unexplained change between the two snapshots


def balance_gaps(con: sqlite3.Connection) -> list[Gap]:
    gaps = []
    for acc in con.execute("SELECT id, name FROM accounts ORDER BY id"):
        snaps = con.execute(
            f"""SELECT as_of, max(amount_cents) amount_cents FROM balances
                WHERE account_id = ? AND balance_type IN ({','.join('?' * len(BOOKED))})
                GROUP BY as_of ORDER BY as_of""",
            (acc["id"], *BOOKED),
        ).fetchall()
        for a, b in pairwise(snaps):
            (moved,) = con.execute(
                """SELECT coalesce(sum(amount_cents), 0) FROM transactions
                   WHERE account_id = ? AND duplicate_of IS NULL AND booking_date > ? AND booking_date <= ?""",
                (acc["id"], a["as_of"], b["as_of"]),
            ).fetchone()
            delta = b["amount_cents"] - a["amount_cents"] - moved
            if delta:
                gaps.append(Gap(acc["name"], a["as_of"], b["as_of"], delta))
    return gaps
