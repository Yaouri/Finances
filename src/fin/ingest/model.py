"""Source-agnostic transaction record that every ingester (API or file) produces."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


@dataclass
class RawTxn:
    booking_date: date
    amount_cents: int  # signed; negative = money out
    currency: str
    description: str
    value_date: date | None = None
    external_id: str | None = None
    counterparty_name: str | None = None
    counterparty_iban: str | None = None
    bank_tx_code: str | None = None
    mcc: str | None = None
    orig_amount_cents: int | None = None
    orig_currency: str | None = None
    balance_after_cents: int | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)
    fingerprint: str = ""


def to_cents(amount: str | float | Decimal) -> int:
    """Parse '1234.56', '-12,30', '1.234,56' or a number into integer cents."""
    if isinstance(amount, (int, float, Decimal)):
        d = Decimal(str(amount))
    else:
        s = amount.strip().replace(" ", "").replace(" ", "").replace("€", "")
        if "," in s and "." in s:
            # whichever separator comes last is the decimal one
            s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")
        d = Decimal(s)
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def norm_text(s: str | None) -> str:
    """Uppercase, accent-free, single-spaced alphanumerics — for hashing and matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).upper()
    return " ".join(s.split())


def assign_fingerprints(txns: list[RawTxn]) -> list[RawTxn]:
    """Stable content hash. Identical (date, amount, text) rows within a batch get an
    occurrence index so genuine repeats (two €0.70 coffees) stay distinct. Batches always
    cover whole days, so the index is stable across overlapping pulls."""
    seen: Counter[tuple] = Counter()
    for t in txns:
        base = (t.booking_date.isoformat(), t.amount_cents, t.currency, norm_text(t.description))
        occ = seen[base]
        seen[base] += 1
        t.fingerprint = hashlib.sha1("|".join(map(str, (*base, occ))).encode()).hexdigest()
    return txns
