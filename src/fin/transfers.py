"""Internal-transfer detection.

1. Pair legs: an outflow and an inflow of the same absolute amount in two different own
   accounts within a few days, both "transfer-like" (transfer/top-up/MB WAY/card to Revolut,
   or counterparty IBAN is ours). Greedy by smallest date gap, one-to-one.
2. Unpaired legs that are still clearly ours (counterparty is an own IBAN, the owner's own
   name, or the Revolut savings pot) are internal too: their twin is in an untracked period.
3. Joint account inflows from someone else = partner contribution (not income).
4. Broker deposits/withdrawals (DEGIRO, Trading 212, ...) = investing, not spending.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .ingest.model import norm_text
from .normalize import descriptors as D

MAX_GAP_DAYS = 4
BROKERS = re.compile(r"\b(DEGIRO|FLATEX|TRADING 212|INTERACTIVE BROKERS|XTB|TRADE REPUBLIC|LIGHTYEAR|SCALABLE)\b")
SAVINGS_POT = re.compile(r"\b(INSTANT ACCESS SAVINGS|POUPANCA DE ACESSO IMEDIATO|SAVINGS|VAULT|COFRE)\b")
TRANSFER_TYPES = {D.TRANSFER, D.TOPUP, D.MBWAY}

INTERNAL, PARTNER = "Internal transfer", "Partner contribution"
DEPOSIT, WITHDRAWAL = "Brokerage deposit", "Brokerage withdrawal"


@dataclass
class Leg:
    id: int
    account_id: int
    account_kind: str
    is_joint: bool
    booking_date: date
    value_date: date | None
    amount_cents: int
    type: str
    party: str  # normalised
    merchant: str | None
    counterparty_iban: str | None


@dataclass
class TransferMark:
    group: str
    category: str  # category path


def _gap(a: Leg, b: Leg) -> int:
    da = [a.booking_date] + ([a.value_date] if a.value_date else [])
    db = [b.booking_date] + ([b.value_date] if b.value_date else [])
    return min(abs((x - y).days) for x in da for y in db)


def detect(legs: list[Leg], own_ibans: set[str], owner_names: set[str]) -> dict[int, TransferMark]:
    owners = [norm_text(n).split() for n in owner_names if n]

    def is_owner(party: str) -> bool:
        """Token-wise prefix match, so truncations like 'DYLAN P' still count; needs >= 2 tokens."""
        p = party.split()
        return len(p) >= 2 and any(
            len(p) <= len(o) and all(o[k].startswith(t) for k, t in enumerate(p)) for o in owners)

    def transfer_like(l: Leg) -> bool:
        return (l.type in TRANSFER_TYPES or l.merchant == "Revolut"
                or (l.counterparty_iban or "") in own_ibans or bool(SAVINGS_POT.search(l.party)))

    out: dict[int, TransferMark] = {}

    # 4. brokers first: never paired with each other, always investing
    for l in legs:
        if BROKERS.search(l.party) and l.type in (D.TRANSFER, D.DIRECT_DEBIT, D.CARD, D.OTHER):
            out[l.id] = TransferMark(f"inv{l.id}", DEPOSIT if l.amount_cents < 0 else WITHDRAWAL)

    # 1. pairing
    cands = [l for l in legs if l.id not in out and transfer_like(l)]
    outs = sorted((l for l in cands if l.amount_cents < 0), key=lambda l: (l.booking_date, l.id))
    ins = [l for l in cands if l.amount_cents > 0]
    used: set[int] = set()
    for o in outs:
        best = None
        for i in ins:
            if i.id in used or i.account_id == o.account_id or i.amount_cents != -o.amount_cents:
                continue
            g = _gap(o, i)
            if g <= MAX_GAP_DAYS and (best is None or g < best[0]):
                best = (g, i)
        if best:
            i = best[1]
            used.add(i.id)
            grp = f"t{o.id}-{i.id}"
            out[o.id] = TransferMark(grp, INTERNAL)
            out[i.id] = TransferMark(grp, INTERNAL)

    # 2./3. unpaired legs
    for l in legs:
        if l.id in out or l.type not in (TRANSFER_TYPES | {D.CARD}):
            continue
        if l.type == D.CARD and l.merchant != "Revolut":
            continue
        own = ((l.counterparty_iban or "") in own_ibans or is_owner(l.party)
               or bool(SAVINGS_POT.search(l.party)) or l.merchant == "Revolut")
        if own:
            out[l.id] = TransferMark(f"u{l.id}", INTERNAL)
        elif l.is_joint and l.amount_cents > 0 and l.type in (D.TRANSFER, D.TOPUP, D.MBWAY):
            out[l.id] = TransferMark(f"p{l.id}", PARTNER)
        elif l.type == D.TOPUP and l.amount_cents > 0:
            # card top-up into a personal account from a card we don't track: own money moving
            out[l.id] = TransferMark(f"u{l.id}", INTERNAL)
    return out
