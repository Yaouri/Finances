"""File imports (historical statements). Format is auto-detected; each batch is routed to an
account by IBAN, explicit override, or overlap with already-synced API data."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from ...db import store_raw
from ..dedupe import insert_transactions
from . import millennium_pdf, revolut, santander_meal
from .base import Batch, ImportFormatError, ImportResult

FORMATS = [revolut, millennium_pdf, santander_meal]

ROLE_KIND = {"savings": "savings", "meal": "meal"}
ROLE_DEFAULT_NAME = {("Revolut", "savings"): "Revolut Savings", ("Santander", "meal"): "Cartão Refeição"}


@dataclass
class BatchOutcome:
    account: str
    fetched: int
    new: int
    existing: int
    duplicates: int


def detect(path: Path):
    for fmt in FORMATS:
        if fmt.detect(path):
            return fmt
    raise ImportFormatError(f"unrecognised file format: {path.name}")


def _overlap_score(con: sqlite3.Connection, account_id: int, b: Batch) -> float:
    """Share of batch rows with a same-amount row within ±3 days already in the account."""
    sample = b.txns[-200:]
    hits = 0
    for t in sample:
        lo = (t.booking_date - timedelta(days=3)).isoformat()
        hi = (t.booking_date + timedelta(days=3)).isoformat()
        hits += bool(con.execute(
            "SELECT 1 FROM transactions WHERE account_id=? AND amount_cents=? AND booking_date BETWEEN ? AND ? LIMIT 1",
            (account_id, t.amount_cents, lo, hi)).fetchone())
    return hits / len(sample) if sample else 0.0


def resolve_account(con: sqlite3.Connection, b: Batch, override: int | None = None) -> int:
    if override is not None:
        return override
    if b.iban:
        row = con.execute("SELECT id FROM accounts WHERE key = ?", (f"iban:{b.iban}",)).fetchone()
        if row:
            return row["id"]
    if b.role in ROLE_KIND:
        row = con.execute(
            "SELECT id FROM accounts WHERE institution LIKE ? AND kind = ? ORDER BY id LIMIT 1",
            (f"{b.institution}%", ROLE_KIND[b.role]),
        ).fetchone()
        if row:
            return row["id"]
        name = ROLE_DEFAULT_NAME.get((b.institution, b.role), f"{b.institution} {b.role}")
        slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower())
        return con.execute(
            "INSERT INTO accounts (key, name, institution, kind, source) VALUES (?,?,?,?, 'manual')",
            (f"manual:{slug}", name, b.institution, ROLE_KIND[b.role]),
        ).lastrowid
    # 'main' without IBAN (Revolut CSVs don't say which account they are): best API overlap
    cands = con.execute(
        "SELECT id, name FROM accounts WHERE institution LIKE ? AND kind <> 'savings'", (f"{b.institution}%",)
    ).fetchall()
    scored = sorted(((_overlap_score(con, c["id"], b), c["id"]) for c in cands), reverse=True)
    if not scored or scored[0][0] < 0.5 or (len(scored) > 1 and scored[1][0] > 0.8 * scored[0][0]):
        names = ", ".join(f"{c['id']}={c['name']}" for c in cands)
        raise ImportFormatError(f"can't tell which {b.institution} account this is; pass --account ({names})")
    return scored[0][1]


def import_file(con: sqlite3.Connection, path: Path, account_id: int | None = None) -> list[BatchOutcome]:
    fmt = detect(path)
    result: ImportResult = fmt.parse(path)
    run_id = con.execute("INSERT INTO sync_runs (source) VALUES (?)", (result.source,)).lastrowid
    raw_id = store_raw(con, "file", path.read_bytes(), sync_run_id=run_id, name=path.name)
    outcomes = []
    try:
        for b in result.batches:
            acc = resolve_account(con, b, account_id if b.role == "main" else None)
            st = insert_transactions(con, acc, result.source, b.txns, raw_payload_id=raw_id)
            for as_of, cents, ccy in b.balances:
                con.execute(
                    """INSERT INTO balances (account_id, as_of, balance_type, amount_cents, currency)
                       VALUES (?,?, 'STATEMENT', ?, ?)
                       ON CONFLICT(account_id, as_of, balance_type) DO UPDATE SET amount_cents = excluded.amount_cents""",
                    (acc, as_of.isoformat(), cents, ccy),
                )
            name = con.execute("SELECT name FROM accounts WHERE id=?", (acc,)).fetchone()["name"]
            outcomes.append(BatchOutcome(name, st.fetched, st.new, st.existing, st.cross_source_dupes))
        con.execute(
            "UPDATE sync_runs SET status='ok', finished_at=datetime('now'), n_fetched=?, n_new=?, n_duplicate=? WHERE id=?",
            (sum(o.fetched for o in outcomes), sum(o.new for o in outcomes),
             sum(o.duplicates for o in outcomes), run_id),
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return outcomes


__all__ = ["import_file", "detect", "ImportFormatError"]
