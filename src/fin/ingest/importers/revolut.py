"""Revolut account statement CSV (app -> Statements -> Excel/CSV). Portuguese or English headers.

One file holds the current account ("Atual"/"Current") *and* the savings sub-account
("Depósito"/"Savings"), which PSD2 does not expose. Amount booked = Montante - Comissão,
which is what the API reports.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from ..model import RawTxn, to_cents
from .base import Batch, ImportResult

COLUMNS = {
    "type": ["Tipo", "Type"],
    "product": ["Produto", "Product"],
    "started": ["Data de início", "Started Date"],
    "completed": ["Data de Conclusão", "Completed Date"],
    "description": ["Descrição", "Description"],
    "amount": ["Montante", "Amount"],
    "fee": ["Comissão", "Fee"],
    "currency": ["Moeda", "Currency"],
    "state": ["Estado", "State"],
    "balance": ["Saldo", "Balance"],
}
DONE = {"CONCLUÍDA", "COMPLETED"}
SAVINGS_PRODUCTS = {"Depósito", "Deposit", "Savings", "Poupança"}


def detect(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    head = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[:1]
    return bool(head) and all(any(c in head[0] for c in names) for names in COLUMNS.values())


def _col(df: pd.DataFrame, key: str) -> str:
    return next(c for c in COLUMNS[key] if c in df.columns)


def parse(path: Path) -> ImportResult:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    c = {k: _col(df, k) for k in COLUMNS}
    df = df[df[c["state"]].str.upper().isin(DONE)].copy()
    df["_start"] = pd.to_datetime(df[c["started"]])
    df = df.sort_values("_start", kind="stable")

    by_role: dict[str, list[RawTxn]] = {"main": [], "savings": []}
    last_balance: dict[str, tuple] = {}
    for _, r in df.iterrows():
        role = "savings" if r[c["product"]] in SAVINGS_PRODUCTS else "main"
        fee = to_cents(r[c["fee"]] or "0")
        cents = to_cents(r[c["amount"]]) - fee
        completed = r[c["completed"]]
        t = RawTxn(
            booking_date=r["_start"].date(),
            value_date=datetime.fromisoformat(completed).date() if completed else None,
            amount_cents=cents,
            currency=r[c["currency"]],
            description=r[c["description"]].strip(),
            bank_tx_code=r[c["type"]],
            balance_after_cents=to_cents(r[c["balance"]]) if r[c["balance"]] else None,
            raw={k: r[v] for k, v in c.items()} | {"fee_cents": fee},
        )
        by_role[role].append(t)
        if t.balance_after_cents is not None:
            last_balance[role] = (r["_start"], t.balance_after_cents, t.currency)

    batches = []
    for role, txns in by_role.items():
        if not txns:
            continue
        bal = last_balance.get(role)
        batches.append(
            Batch(
                role=role,
                institution="Revolut",
                txns=txns,
                balances=[(bal[0].date(), bal[1], bal[2])] if role == "savings" and bal else [],
            )
        )
    return ImportResult(source="csv:revolut", batches=batches)
