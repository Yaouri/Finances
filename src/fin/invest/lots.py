"""Lot table: DEGIRO 'Transactions.csv' import + manual lots (e.g. Millennium-era purchases).

DEGIRO books an in-specie transfer-in as a trade without an order id, priced at market value on
the transfer date. It is imported as kind='transfer_in'; once the original purchase lots are
added (with their real dates and cost), it is marked superseded so cost basis and history use
the originals instead.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

# ISIN -> (name, yfinance ticker, currency). EUR listings avoid FX conversion.
KNOWN = {
    "IE00BK5BQT80": ("Vanguard FTSE All-World (Acc)", "VWCE.DE", "EUR", "etf"),
    "US02079K1079": ("Alphabet Inc. Class C", "ABEC.DE", "EUR", "equity"),
    "US67066G1040": ("NVIDIA Corporation", "NVD.DE", "EUR", "equity"),
    "US92766K4031": ("Virgin Galactic Holdings", "SPCE", "USD", "equity"),
}

COLS = {  # Portuguese / English DEGIRO exports
    "date": ["Data", "Date"], "time": ["Hora", "Time"], "product": ["Produto", "Product"], "isin": ["ISIN"],
    "qty": ["Quantidade", "Quantity"], "price": ["Preços", "Price"], "local": ["Valor local", "Local value"],
    "autofx": ["Taxa Autofx", "AutoFX Fee"],
    "fees": ["Custos de transação e/ou taxas de terceiros", "Transaction and/or third party fees EUR",
             "Transaction costs"],
    "total": ["Total EUR", "Total"], "order": ["ID da Ordem", "Order ID"],
}


def detect(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    head = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[:1]
    return bool(head) and "ISIN" in head[0] and any(o in head[0] for o in COLS["order"])


def _num(v) -> float:
    """'-48261,48' / '171,1400' / '1.234,5' -> float, keeping full precision (prices have 4 dp)."""
    s = "" if v is None else str(v).strip()
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def parse_degiro(path: Path) -> list[dict]:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    c = {k: next(n for n in names if n in df.columns) for k, names in COLS.items() if any(n in df.columns for n in names)}
    # the currency of 'Preços' / 'Valor local' sits in the unnamed column right after each
    cols = list(df.columns)
    price_ccy = cols[cols.index(c["price"]) + 1]
    out = []
    fills: dict[str, int] = {}
    for _, r in df.iterrows():
        qty = float(_num(r[c["qty"]]))
        fees = abs(_num(r[c["fees"]])) + abs(_num(r.get(c.get("autofx", ""), 0)))
        order = r[c["order"]].strip() or None
        d = datetime.strptime(r[c["date"]], "%d-%m-%Y").date()
        kind = "trade" if order else "transfer_in"
        # partial fills share the order id, and identical fills can repeat: add an occurrence index
        base = f"{order or 'xfer'}|{d}|{r[c['time']]}|{r[c['isin']]}|{qty}|{r[c['price']]}"
        n = fills[base] = fills.get(base, -1) + 1
        ext = hashlib.sha1(f"{base}|{n}".encode()).hexdigest()[:16]
        out.append({
            "trade_date": d.isoformat(), "trade_time": r[c["time"]], "isin": r[c["isin"]], "product": r[c["product"]],
            "quantity": qty, "price": _num(r[c["price"]]), "currency": r[price_ccy] or "EUR", "fees": round(fees, 2),
            "total_eur": _num(r[c["total"]]), "external_id": ext, "kind": kind,
            "note": "in-specie transfer: cost = market value on transfer date until original lots are added"
            if kind == "transfer_in" else None,
        })
    return out


def ensure_instrument(con: sqlite3.Connection, isin: str, name: str) -> None:
    name_, ticker, ccy, cls = KNOWN.get(isin, (name, None, "EUR", "equity"))
    con.execute("INSERT OR IGNORE INTO instruments (isin, name, ticker, currency, asset_class) VALUES (?,?,?,?,?)",
                (isin, name_, ticker, ccy, cls))


def import_degiro(con: sqlite3.Connection, path: Path) -> dict:
    rows = parse_degiro(path)
    new = updated = skipped = 0
    for r in rows:
        ensure_instrument(con, r["isin"], r["product"])
        ticker = con.execute("SELECT ticker FROM instruments WHERE isin=?", (r["isin"],)).fetchone()[0]
        cur = con.execute("SELECT id, locked FROM lots WHERE external_id = ?", (r["external_id"],)).fetchone()
        vals = (r["trade_date"], r["trade_time"], r["isin"], ticker or r["isin"], r["quantity"], r["price"],
                r["fees"], r["currency"], r["total_eur"], r["kind"], r["note"])
        if cur is None:
            con.execute(
                """INSERT INTO lots (trade_date, trade_time, isin, ticker, quantity, price, fees, currency, total_eur,
                                     kind, note, broker, source, external_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, 'DEGIRO', 'degiro_csv', ?)""",
                (*vals, r["external_id"]),
            )
            new += 1
        elif cur["locked"]:
            skipped += 1
        else:
            con.execute(
                """UPDATE lots SET trade_date=?, trade_time=?, isin=?, ticker=?, quantity=?, price=?, fees=?,
                                   currency=?, total_eur=?, kind=?, note=coalesce(note, ?) WHERE id=?""",
                (*vals, cur["id"]),
            )
            updated += 1
    con.commit()
    return {"rows": len(rows), "new": new, "updated": updated, "locked_skipped": skipped}


def add_lot(con: sqlite3.Connection, *, trade_date: str, isin: str, quantity: float, price: float, fees: float = 0,
            broker: str = "Millennium BCP", note: str | None = None, name: str | None = None) -> int:
    """Manual lot (always locked). Cost in EUR = quantity * price + fees."""
    ensure_instrument(con, isin, name or isin)
    ticker = con.execute("SELECT ticker FROM instruments WHERE isin=?", (isin,)).fetchone()[0]
    total = -(quantity * price + fees) if quantity > 0 else (-quantity * price - fees)
    lot_id = con.execute(
        """INSERT INTO lots (trade_date, isin, ticker, quantity, price, fees, currency, total_eur, kind, broker,
                             source, locked, note)
           VALUES (?,?,?,?,?,?, 'EUR', ?, 'trade', ?, 'manual', 1, ?)""",
        (trade_date, isin, ticker or isin, quantity, price, fees, round(total, 2), broker, note),
    ).lastrowid
    con.commit()
    return lot_id
