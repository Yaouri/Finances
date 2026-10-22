"""Manual decisions (stored in tx_user / tx_tags, never touched by reprocessing)."""

from __future__ import annotations

import re
import sqlite3

from . import rules as R
from . import taxonomy

ONE_OFF = "one-off"  # tag: counts in net worth, excluded from income/expense and savings-rate metrics


def set_category(
    con: sqlite3.Connection,
    tx_id: int,
    category_path: str | None = None,
    *,
    note: str | None = None,
    tags: tuple[str, ...] = (),
    my_share: float | None = None,
    make_rule: bool = False,
) -> int | None:
    """Recategorise one transaction. With make_rule, also add a merchant rule so future ones follow.
    Returns the new rule id, if any."""
    cat = None
    if category_path:
        cat = taxonomy.path_ids(con).get(category_path)
        if cat is None:
            raise ValueError(f"unknown category {category_path!r}")
    con.execute(
        """INSERT INTO tx_user (tx_id, category_id, notes, my_share, reviewed) VALUES (?,?,?,?,1)
           ON CONFLICT(tx_id) DO UPDATE SET
             category_id = coalesce(excluded.category_id, tx_user.category_id),
             notes = coalesce(excluded.notes, tx_user.notes),
             my_share = coalesce(excluded.my_share, tx_user.my_share),
             reviewed = 1, updated_at = datetime('now')""",
        (tx_id, cat, note, my_share),
    )
    for t in tags:
        con.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
        con.execute("INSERT OR IGNORE INTO tx_tags (tx_id, tag_id, source) SELECT ?, id, 'manual' FROM tags WHERE name=?",
                    (tx_id, t))
    rule_id = None
    if make_rule and cat:
        m = con.execute(
            "SELECT m.name FROM tx_enriched e JOIN merchants m ON m.id = e.merchant_id WHERE e.tx_id = ?", (tx_id,)
        ).fetchone()
        if m is None:
            raise ValueError("transaction has no merchant to build a rule from")
        rule_id = R.add_rule(con, "merchant", f"^{re.escape(m['name'])}$", cat, name=f"from tx {tx_id}")
    con.commit()
    return rule_id


def set_tags(con: sqlite3.Connection, tx_id: int, tags: list[str]) -> None:
    """Replace the manual tags of a transaction (rule-applied tags are left alone)."""
    con.execute("DELETE FROM tx_tags WHERE tx_id = ? AND source = 'manual'", (tx_id,))
    for t in {t.strip() for t in tags if t.strip()}:
        con.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
        con.execute("INSERT OR IGNORE INTO tx_tags (tx_id, tag_id, source) SELECT ?, id, 'manual' FROM tags WHERE name=?",
                    (tx_id, t))
    con.commit()


def tag_range(con: sqlite3.Connection, tag: str, start: str, end: str, account_ids: list[int],
              flows: tuple[str, ...] = ("expense",)) -> int:
    """Tag every transaction in a date range (e.g. a trip). Returns how many were tagged."""
    con.execute("INSERT OR IGNORE INTO tags (name, kind) VALUES (?, 'trip')", (tag,))
    q = f"""INSERT OR IGNORE INTO tx_tags (tx_id, tag_id, source)
            SELECT t.id, (SELECT id FROM tags WHERE name = ?), 'manual'
            FROM transactions t JOIN tx_enriched e ON e.tx_id = t.id
            WHERE t.duplicate_of IS NULL AND t.booking_date BETWEEN ? AND ?
              AND e.flow IN ({','.join('?' * len(flows))})
              {"AND t.account_id IN (" + ','.join('?' * len(account_ids)) + ")" if account_ids else ""}"""
    n = con.execute(q, (tag, start, end, *flows, *account_ids)).rowcount
    con.commit()
    return n
