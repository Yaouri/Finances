"""Millennium BCP monthly 'EXTRATO COMBINADO' PDF.

Text extraction loses the DEBITO/CREDITO columns, so each movement's signed amount is derived
from the running balance (SALDO) delta, and the parse must reconcile to SALDO FINAL.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pdfplumber

from ..model import RawTxn, to_cents
from .base import Batch, ImportFormatError, ImportResult

AMT = r"\d{1,3}(?: \d{3})*\.\d{2}"
RE_PERIOD = re.compile(r"EXTRATO DE (\d{4})/(\d{2})/(\d{2}) A (\d{4})/(\d{2})/(\d{2})")
RE_IBAN = re.compile(r"IBAN:\s*(PT50[\d ]+)")
# lazy description: the balance is the longest trailing amount ("41 978.14", not "978.14")
RE_MOVE = re.compile(rf"^(\d{{1,2}})\.(\d{{2}}) (\d{{1,2}})\.(\d{{2}}) (.+?) ({AMT})$")
RE_OPEN = re.compile(rf"^(?:SALDO INICIAL|TRANSPORTE) ({AMT})$")
RE_FINAL = re.compile(rf"^SALDO FINAL ({AMT})$")
# securities block: "<NAME> <QTY> EUR <PRICE> 0 0 <VALUE>" followed by "(ISIN: XX...)"
RE_HOLDING = re.compile(rf"^(.+?) (\d+\.\d+) (EUR) ([\d.]+) .*? ({AMT})$")
RE_ISIN = re.compile(r"\(ISIN: ([A-Z]{2}[A-Z0-9]{10})\)")


def detect(path: Path) -> bool:
    if path.suffix.lower() != ".pdf":
        return False
    try:
        with pdfplumber.open(path) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception:  # noqa: BLE001 - detection must never raise
        return False
    return "EXTRATO COMBINADO" in text and "BCOMPTPL" in text


def _year_for(month: int, start: date, end: date) -> int:
    return start.year if month >= start.month or start.year == end.year else end.year


def _split_amount(rest: str, delta: int) -> tuple[str, int]:
    """`rest` is '<description> <amount>'; pick the split whose amount equals |delta|.
    Needed because descriptions may end in digit groups ('... 123 456.00')."""
    for pos in (m.end() for m in re.finditer(" ", rest)):
        tail = rest[pos:]
        if re.fullmatch(AMT, tail) and (amt := to_cents(tail)) == abs(delta):
            return rest[:pos].strip(), amt
    raise ImportFormatError(f"no amount in {rest!r} matches balance delta {delta / 100:.2f}")


def parse(path: Path) -> ImportResult:
    with pdfplumber.open(path) as pdf:
        lines = [ln.strip() for p in pdf.pages for ln in (p.extract_text() or "").splitlines()]
    return parse_lines(lines)


def parse_lines(lines: list[str]) -> ImportResult:
    text = "\n".join(lines)
    pm = RE_PERIOD.search(text)
    im = RE_IBAN.search(text)
    if not pm or not im:
        raise ImportFormatError("period or IBAN header not found")
    start = date(int(pm[1]), int(pm[2]), int(pm[3]))
    end = date(int(pm[4]), int(pm[5]), int(pm[6]))
    iban = im[1].replace(" ", "")

    txns: list[RawTxn] = []
    balance: int | None = None
    final: int | None = None
    holdings = []
    in_securities = False
    for i, ln in enumerate(lines):
        if m := RE_OPEN.match(ln):
            balance = to_cents(m[1])
            continue
        if m := RE_FINAL.match(ln):
            final = to_cents(m[1])
            continue
        if ln.startswith("CONTA TITULOS"):
            in_securities = True
            continue
        if in_securities:
            if (m := RE_HOLDING.match(ln)) and i + 1 < len(lines) and (isin := RE_ISIN.search(lines[i + 1])):
                holdings.append({"name": m[1], "isin": isin[1], "quantity": float(m[2]), "currency": m[3],
                                 "price": float(m[4]), "value_cents": to_cents(m[5]), "as_of": end.isoformat()})
            continue
        m = RE_MOVE.match(ln)
        if not m or balance is None:
            continue
        new_balance = to_cents(m[6])
        delta = new_balance - balance
        desc, amt = _split_amount(m[5], delta)
        bm, bd, vm, vd = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        txns.append(
            RawTxn(
                booking_date=date(_year_for(bm, start, end), bm, bd),
                value_date=date(_year_for(vm, start, end), vm, vd),
                amount_cents=amt if delta > 0 else -amt,
                currency="EUR",
                description=desc,
                balance_after_cents=new_balance,
                raw={"line": ln},
            )
        )
        balance = new_balance

    if final is None or balance != final:
        raise ImportFormatError(f"did not reconcile: parsed balance {balance} vs SALDO FINAL {final}")
    return ImportResult(
        source="pdf:millennium",
        batches=[Batch(role="main", institution="Millennium BCP", iban=iban, txns=txns,
                       balances=[(end, final, "EUR")], extra={"holdings": holdings})],
    )
