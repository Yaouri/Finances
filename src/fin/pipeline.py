"""Deterministic reprocessing: raw transactions -> tx_enriched (+ rule tags).

Priority per transaction: manual (tx_user) > transfer detection > rules > (ML, later) > none.
Safe to run any time; it never touches raw data or manual decisions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from . import transfers
from .categorize import rules as R
from .categorize import taxonomy
from .db import get_setting, set_setting, tx
from .ingest.model import norm_text
from .normalize import descriptors
from .normalize.merchants import MerchantResolver


@dataclass
class Stats:
    total: int = 0
    manual: int = 0
    transfer: int = 0
    rule: int = 0
    ml: int = 0
    uncategorised: int = 0


def owner_names(con: sqlite3.Connection) -> set[str]:
    names = get_setting(con, "owner_names")
    if names is None:
        # bootstrap from the holder names the banks report on non-joint accounts
        names = sorted({
            (json.loads(r["eb_raw"]).get("name") or "") for r in con.execute(
                "SELECT eb_raw FROM accounts WHERE eb_raw IS NOT NULL AND is_joint = 0")
        } - {""})
        set_setting(con, "owner_names", names)
    return set(names)


def reprocess(con: sqlite3.Connection) -> Stats:
    taxonomy.seed(con)
    R.seed(con)
    paths = taxonomy.path_ids(con)
    kinds = taxonomy.kinds(con)
    rules = R.load(con)
    resolver = MerchantResolver(con)
    accounts = {r["id"]: r for r in con.execute("SELECT * FROM accounts")}
    own_ibans = {r["iban"] for r in con.execute("SELECT iban FROM own_ibans")}
    owners = owner_names(con)

    rows = con.execute(
        """SELECT t.*, u.category_id AS u_cat, u.merchant_name AS u_merchant, u.my_share AS u_share
           FROM transactions t LEFT JOIN tx_user u ON u.tx_id = t.id
           WHERE t.duplicate_of IS NULL ORDER BY t.booking_date, t.id"""
    ).fetchall()

    items = []
    for r in rows:
        acc = accounts[r["account_id"]]
        p = descriptors.parse(r["description"], r["bank_tx_code"], acc["institution"])
        key, mid, mname = resolver.resolve(p.party) if p.type not in (descriptors.FEE, descriptors.FX) else ("", None, None)
        ctx = {
            "description": r["description"], "merchant": mname or "", "party": norm_text(p.party),
            "type": p.type, "counterparty": r["counterparty_name"] or "", "amount_cents": r["amount_cents"],
            "account_id": r["account_id"], "account_kind": acc["kind"],
        }
        items.append((r, acc, p, key, mid, ctx))

    legs = [
        transfers.Leg(
            id=r["id"], account_id=r["account_id"], account_kind=acc["kind"], is_joint=bool(acc["is_joint"]),
            booking_date=date.fromisoformat(r["booking_date"]),
            value_date=date.fromisoformat(r["value_date"]) if r["value_date"] else None,
            amount_cents=r["amount_cents"], type=p.type, party=ctx["party"], merchant=ctx["merchant"] or None,
            counterparty_iban=(r["counterparty_iban"] or "").replace(" ", "").upper() or None,
        )
        for r, acc, p, key, mid, ctx in items
    ]
    marks = transfers.detect(legs, own_ibans, owners)

    st = Stats(total=len(items))
    rows_out, rule_tags = {}, []
    for r, acc, p, key, mid, ctx in items:
        cat, source, conf, rule_id, group = None, "none", None, None, None
        mark = marks.get(r["id"])
        if mark:
            group = mark.group
        if r["u_cat"]:
            cat, source, conf = r["u_cat"], "manual", 1.0
            st.manual += 1
        elif mark:
            cat, source, conf = paths[mark.category], "transfer", 1.0
            st.transfer += 1
        elif rule := R.first_match(rules, ctx):
            cat, source, conf, rule_id = rule.category_id, "rule", 1.0, rule.id
            if rule.merchant_name:
                mid = resolver.merchant_id(rule.merchant_name)
            rule_tags += [(r["id"], t) for t in rule.tags]
            st.rule += 1
        if r["u_merchant"]:
            mid = resolver.merchant_id(r["u_merchant"])
        rows_out[r["id"]] = {
            "r": r, "acc": acc, "type": p.type, "key": key or None, "mid": mid, "cat": cat, "source": source,
            "conf": conf, "rule_id": rule_id, "group": group, "ml_cat": None, "ml_conf": None,
            "merchant": ctx["merchant"],
        }

    _ml_pass(rows_out, kinds, st)

    enriched = []
    for tx_id, o in rows_out.items():
        r, acc, cat = o["r"], o["acc"], o["cat"]
        flow = kinds.get(cat) if cat else ("expense" if r["amount_cents"] < 0 else "income")
        share = r["u_share"] if r["u_share"] is not None else (acc["my_share"] if acc["is_joint"] else 1.0)
        amount_eur = r["amount_cents"] if r["currency"] == "EUR" else None  # FX conversion: TODO with fx_rates
        enriched.append((
            tx_id, o["type"], o["key"], o["mid"], cat, o["source"], o["conf"], o["rule_id"], o["group"], flow,
            amount_eur, round(amount_eur * share) if amount_eur is not None else None, o["ml_cat"], o["ml_conf"],
        ))

    with tx(con):
        con.execute("DELETE FROM tx_enriched")
        con.executemany(
            """INSERT INTO tx_enriched (tx_id, descriptor_type, merchant_key, merchant_id, category_id,
                   category_source, confidence, rule_id, transfer_group, flow, amount_eur_cents, my_amount_eur_cents,
                   ml_category_id, ml_confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            enriched,
        )
        con.execute("DELETE FROM tx_tags WHERE source = 'rule'")
        for tx_id, tag in rule_tags:
            con.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
            con.execute(
                "INSERT OR IGNORE INTO tx_tags (tx_id, tag_id, source) SELECT ?, id, 'rule' FROM tags WHERE name = ?",
                (tx_id, tag),
            )
    return st


def _ml_pass(rows: dict, kinds: dict, st: Stats) -> None:
    """Train on what rules/manual labelled this run; fill or suggest the rest."""
    import pandas as pd

    from .categorize import ml

    def frame(ids):
        return pd.DataFrame([{
            "merchant": rows[i]["merchant"], "description": rows[i]["r"]["description"], "type": rows[i]["type"],
            "account_kind": rows[i]["acc"]["kind"], "amount_cents": rows[i]["r"]["amount_cents"],
            "category_id": rows[i]["cat"], "source": rows[i]["source"],
        } for i in ids], index=list(ids))

    labelled = [i for i, o in rows.items() if o["source"] in ("manual", "rule") and kinds.get(o["cat"]) in ("expense", "income")]
    todo = [i for i, o in rows.items() if o["cat"] is None]
    preds = ml.train_and_predict(frame(labelled), frame(todo)) if labelled and todo else {}
    for i, pr in preds.items():
        o = rows[i]
        if pr.confidence >= ml.ACCEPT:
            o["cat"], o["source"], o["conf"] = pr.category_id, "ml", pr.confidence
            st.ml += 1
        else:
            o["ml_cat"], o["ml_conf"] = pr.category_id, pr.confidence
    st.uncategorised = sum(1 for o in rows.values() if o["cat"] is None)


def reseed(con: sqlite3.Connection) -> None:
    """Rebuild seeded taxonomy/rules and derived merchant aliases. Keeps user rules; refuses to
    drop categories that manual edits or user rules point at."""
    used = con.execute(
        "SELECT count(*) FROM tx_user WHERE category_id IS NOT NULL"
    ).fetchone()[0] + con.execute("SELECT count(*) FROM rules WHERE coalesce(name, '') <> 'seed'").fetchone()[0]
    with tx(con):
        con.execute("DELETE FROM tx_enriched")
        con.execute("DELETE FROM rules WHERE name = 'seed'")
        con.execute("DELETE FROM merchant_aliases")
        con.execute("DELETE FROM merchants WHERE id NOT IN (SELECT merchant_id FROM tx_enriched WHERE merchant_id IS NOT NULL)")
        if not used:
            con.execute("DELETE FROM categories")
    taxonomy.seed(con)
    R.seed(con)
