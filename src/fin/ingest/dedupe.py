"""Idempotent insert with three layers of identity:

1. same source + bank transaction id          -> already have it
2. same account + content fingerprint         -> already have it
3. other source, same amount, date ±N days    -> same real-world transaction (e.g. CSV vs API);
   stored, but marked duplicate_of the earlier row so it is excluded from analysis.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from rapidfuzz import fuzz

from .model import RawTxn, assign_fingerprints, norm_text

DATE_WINDOW_DAYS = 3


@dataclass
class InsertStats:
    fetched: int = 0
    new: int = 0
    existing: int = 0
    cross_source_dupes: int = 0


def _cross_source_candidates(
    con: sqlite3.Connection, account_id: int, source: str, t: RawTxn
) -> list[tuple[float, int]]:
    """(score, id) of unclaimed rows from other sources that could be the same transaction."""
    lo = (t.booking_date - timedelta(days=DATE_WINDOW_DAYS)).isoformat()
    hi = (t.booking_date + timedelta(days=DATE_WINDOW_DAYS)).isoformat()
    rows = con.execute(
        """
        SELECT c.id, c.booking_date, c.value_date, c.description, c.counterparty_name
        FROM transactions c
        WHERE c.account_id = ? AND c.source <> ? AND c.amount_cents = ?
          AND c.booking_date BETWEEN ? AND ?
          AND c.duplicate_of IS NULL
          AND NOT EXISTS (SELECT 1 FROM transactions d WHERE d.duplicate_of = c.id)
        """,
        (account_id, source, t.amount_cents, lo, hi),
    ).fetchall()
    mine = norm_text(f"{t.description} {t.counterparty_name or ''}")
    mine_dates = [t.booking_date.isoformat()] + ([t.value_date.isoformat()] if t.value_date else [])

    def score(r) -> float:
        dates = [r["booking_date"]] + ([r["value_date"]] if r["value_date"] else [])
        exact_date = any(a == b for a in dates for b in mine_dates)
        gap = min(abs((date.fromisoformat(a) - date.fromisoformat(b)).days) for a in dates for b in mine_dates)
        text = fuzz.token_set_ratio(mine, norm_text(f"{r['description']} {r['counterparty_name'] or ''}"))
        return (50 if exact_date else 0) - 10 * gap + 0.5 * text

    return [(score(r), r["id"]) for r in rows]


def insert_transactions(
    con: sqlite3.Connection,
    account_id: int,
    source: str,
    txns: list[RawTxn],
    raw_payload_id: int | None = None,
) -> InsertStats:
    stats = InsertStats(fetched=len(txns))
    assign_fingerprints(txns)

    # pass 1: exact identity (bank id or fingerprint). Rows hit here are "claimed" so the fuzzy
    # pass can't hand them to a neighbouring same-amount transaction.
    claimed: set[int] = set()
    pending: list[RawTxn] = []
    for t in txns:
        hit = None
        if t.external_id:
            hit = con.execute(
                "SELECT id FROM transactions WHERE account_id=? AND source=? AND external_id=?",
                (account_id, source, t.external_id),
            ).fetchone()
        hit = hit or con.execute(
            "SELECT id FROM transactions WHERE account_id=? AND fingerprint=?", (account_id, t.fingerprint)
        ).fetchone()
        if hit:
            claimed.add(hit[0])
            stats.existing += 1
        else:
            pending.append(t)

    # pass 2: cross-source matches, best-scoring pairs first, one-to-one
    options = [(sc, i, cid) for i, t in enumerate(pending)
               for sc, cid in _cross_source_candidates(con, account_id, source, t) if cid not in claimed]
    dup_of: dict[int, int] = {}
    for _, i, cid in sorted(options, reverse=True):
        if i not in dup_of and cid not in claimed:
            dup_of[i] = cid
            claimed.add(cid)

    for i, t in enumerate(pending):
        con.execute(
            """
            INSERT INTO transactions (
                account_id, source, external_id, fingerprint, booking_date, value_date,
                amount_cents, currency, orig_amount_cents, orig_currency, description,
                counterparty_name, counterparty_iban, bank_tx_code, mcc, balance_after_cents,
                raw_json, raw_payload_id, duplicate_of)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                account_id, source, t.external_id, t.fingerprint, t.booking_date.isoformat(),
                t.value_date.isoformat() if t.value_date else None, t.amount_cents, t.currency,
                t.orig_amount_cents, t.orig_currency, t.description, t.counterparty_name,
                t.counterparty_iban, t.bank_tx_code, t.mcc, t.balance_after_cents,
                json.dumps(t.raw, ensure_ascii=False, default=str) if t.raw is not None else None,
                raw_payload_id, dup_of.get(i),
            ),
        )
        if i in dup_of:
            stats.cross_source_dupes += 1
        else:
            stats.new += 1
    return stats
