"""Ordered pattern rules: first match wins."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import taxonomy

SEED_FILE = Path(__file__).with_name("seed_rules.yaml")
FIELDS = ("description", "merchant", "party", "type", "counterparty")


@dataclass
class Rule:
    id: int
    field: str
    regex: re.Pattern
    category_id: int | None
    account_id: int | None = None
    account_kind: str | None = None
    sign: int | None = None
    min_cents: int | None = None
    max_cents: int | None = None
    merchant_name: str | None = None
    tags: tuple[str, ...] = ()

    def matches(self, ctx: dict) -> bool:
        if self.account_id is not None and ctx["account_id"] != self.account_id:
            return False
        if self.account_kind and ctx["account_kind"] != self.account_kind:
            return False
        amt = ctx["amount_cents"]
        if self.sign is not None and (amt > 0) != (self.sign > 0):
            return False
        if self.min_cents is not None and abs(amt) < self.min_cents:
            return False
        if self.max_cents is not None and abs(amt) > self.max_cents:
            return False
        return bool(self.regex.search(ctx.get(self.field) or ""))


def load(con: sqlite3.Connection) -> list[Rule]:
    out = []
    for r in con.execute("SELECT * FROM rules WHERE enabled = 1 ORDER BY priority, id"):
        try:
            rx = re.compile(r["pattern"], re.IGNORECASE)
        except re.error:
            continue  # invalid user regex: skip rather than break the pipeline (the editor validates)
        out.append(Rule(
            id=r["id"], field=r["field"], regex=rx, category_id=r["category_id"], account_id=r["account_id"],
            account_kind=r["account_kind"], sign=r["sign"], min_cents=r["min_cents"], max_cents=r["max_cents"],
            merchant_name=r["merchant_name"],
            tags=tuple(t.strip() for t in (r["tags"] or "").split(",") if t.strip()),
        ))
    return out


def first_match(rules: list[Rule], ctx: dict) -> Rule | None:
    return next((r for r in rules if r.matches(ctx)), None)


def seed(con: sqlite3.Connection) -> int:
    if con.execute("SELECT 1 FROM rules LIMIT 1").fetchone():
        return 0
    ids = taxonomy.path_ids(con)
    items = yaml.safe_load(SEED_FILE.read_text(encoding="utf-8"))
    for i, it in enumerate(items):
        if it["field"] not in FIELDS:
            raise ValueError(f"seed rule {i}: unknown field {it['field']}")
        re.compile(it["pattern"])
        cat = ids[it["category"]]  # KeyError = typo in seed file; fail loudly
        cents = {k: round(it[f"{k}_eur"] * 100) if f"{k}_eur" in it else None for k in ("min", "max")}
        con.execute(
            """INSERT INTO rules (priority, field, pattern, account_kind, sign, min_cents, max_cents, category_id, name)
               VALUES (?,?,?,?,?,?,?,?, 'seed')""",
            (1000 + i * 10, it["field"], it["pattern"], it.get("account_kind"), it.get("sign"),
             cents["min"], cents["max"], cat),
        )
    con.commit()
    return len(items)


def add_rule(
    con: sqlite3.Connection, field: str, pattern: str, category_id: int, *, priority: int = 100, **kw
) -> int:
    """User rules default to priority 100, i.e. ahead of every seed rule."""
    if field not in FIELDS:
        raise ValueError(f"field must be one of {FIELDS}")
    re.compile(pattern)
    cols = {"priority": priority, "field": field, "pattern": pattern, "category_id": category_id} | kw
    con.execute(f"INSERT INTO rules ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]
