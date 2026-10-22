"""DEGIRO 'Account statement' CSV (Account.csv): cash balance history + dividends/interest.

Not imported as transactions: deposits are already the investing leg on the bank side, and trades
are in Transactions.csv. What this adds is (a) the broker cash balance for net worth and (b)
investment income. Rows are newest-first; the first EUR row seen for a day is its closing balance.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from .lots import _num

ACCOUNT_KEY = "manual:degiro-cash"
INCOME = {  # description prefix -> kind
    "Dividendo": "dividend", "Dividend": "dividend",
    "Imposto sobre dividendo": "dividend_tax", "Dividend Tax": "dividend_tax",
    "Flatex Interest Income": "interest", "Flatex Interest": "interest", "Juros": "interest",
}


def detect(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    head = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[:1]
    return bool(head) and head[0].startswith(("Data,Hora,Data Valor", "Date,Time,Value date"))


def parse(path: Path) -> tuple[list[tuple[str, int]], list[dict]]:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    cols = list(df.columns)
    desc_c = cols[5]
    change_ccy, change, bal_ccy, bal = cols[7], cols[8], cols[9], cols[10]
    # 'Mudança' holds the currency and the next (unnamed) column the amount; same for 'Saldo'
    balances: dict[str, int] = {}
    income = []
    for _, r in df.iterrows():
        d = datetime.strptime(r[cols[0]], "%d-%m-%Y").date().isoformat()
        if r[bal_ccy] == "EUR" and r[bal] and d not in balances:
            balances[d] = round(_num(r[bal]) * 100)
        desc = r[desc_c].strip()
        kind = next((k for p, k in INCOME.items() if desc == p or desc.startswith(p + " ")), None)
        if kind and r[change]:
            amount = _num(r[change])
            if amount:
                income.append({"date": d, "isin": r[cols[4]] or None, "kind": kind, "amount": amount,
                               "currency": r[change_ccy] or "EUR"})
    return sorted(balances.items()), income


def import_account(con: sqlite3.Connection, path: Path) -> dict:
    balances, income = parse(path)
    acc = con.execute("SELECT id FROM accounts WHERE key = ?", (ACCOUNT_KEY,)).fetchone()
    acc_id = acc[0] if acc else con.execute(
        "INSERT INTO accounts (key, name, institution, kind, source) VALUES (?, 'DEGIRO cash', 'DEGIRO', 'investment', 'manual')",
        (ACCOUNT_KEY,),
    ).lastrowid
    con.executemany(
        """INSERT INTO balances (account_id, as_of, balance_type, amount_cents, currency) VALUES (?,?, 'STATEMENT', ?, 'EUR')
           ON CONFLICT(account_id, as_of, balance_type) DO UPDATE SET amount_cents = excluded.amount_cents""",
        [(acc_id, d, c) for d, c in balances],
    )
    con.execute("DELETE FROM invest_income WHERE source = 'degiro_account'")
    con.executemany(
        "INSERT INTO invest_income (date, isin, kind, amount, currency, source) VALUES (?,?,?,?,?, 'degiro_account')",
        [(i["date"], i["isin"], i["kind"], i["amount"], i["currency"]) for i in income],
    )
    con.commit()
    return {"balance_days": len(balances), "latest": balances[-1] if balances else None, "income_rows": len(income)}
