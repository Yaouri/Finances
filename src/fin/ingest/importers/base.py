from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..model import RawTxn


@dataclass
class Batch:
    """Transactions for one account. The account is resolved from `iban` if given,
    otherwise from `role` (main | savings | meal) within `institution`."""

    role: str
    institution: str
    txns: list[RawTxn]
    iban: str | None = None
    # (as_of, amount_cents, currency) snapshots found in the file
    balances: list[tuple[date, int, str]] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class ImportResult:
    source: str
    batches: list[Batch]


class ImportFormatError(ValueError):
    pass
