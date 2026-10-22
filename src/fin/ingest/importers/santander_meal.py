"""Santander Cartão Refeição: NetBanco 'Consultar Movimentos de Cartão' export (descarga.xls)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from ..model import RawTxn, to_cents
from .base import Batch, ImportFormatError, ImportResult


def _read(path: Path) -> pd.DataFrame:
    return pd.read_excel(path, header=None, dtype=str)


def detect(path: Path) -> bool:
    if path.suffix.lower() not in (".xls", ".xlsx"):
        return False
    try:
        head = _read(path).head(10).fillna("").astype(str).agg(" ".join, axis=1).str.cat(sep=" ")
    except Exception:  # noqa: BLE001 - detection must never raise
        return False
    return "Movimentos de Cartão" in head and "Data Operação" in head


def parse(path: Path) -> ImportResult:
    raw = _read(path).fillna("")
    hdr = raw.index[raw[0].str.strip() == "Data Operação"]
    if hdr.empty:
        raise ImportFormatError("header row 'Data Operação' not found")
    card = next((v for v in raw[0].head(10) if "Cartão" in v and "X" in v), "")
    rows = raw.iloc[hdr[0] + 1 :]

    txns = []
    for _, r in rows.iterrows():
        if not r[0].strip():
            continue
        amount = r[3].replace("EUR", "").strip()
        txns.append(
            RawTxn(
                booking_date=datetime.strptime(r[0].strip(), "%d-%m-%Y").date(),
                value_date=datetime.strptime(r[1].strip(), "%d-%m-%Y").date() if r[1].strip() else None,
                amount_cents=to_cents(amount),
                currency="EUR",
                description=r[2].strip(),
                raw={"operacao": r[0], "valor": r[1], "descricao": r[2], "montante": r[3]},
            )
        )
    txns.reverse()  # file is newest-first; keep chronological order for stable fingerprints
    return ImportResult(
        source="xls:santander_meal",
        batches=[Batch(role="meal", institution="Santander", txns=txns, extra={"card": card.strip()})],
    )
