"""Parse bank descriptors into (type, merchant/counterparty text).

Millennium packs everything into the description ("COMPRA 3043 PINGO DOCE LISBOA PT CONTACTLESS",
"TRF MB WAY P/ JOANA", "DD AHC 00001535 PT51116383"); Revolut gives a clean merchant plus a
bank transaction code. The API turns "/" into spaces ("TRF MB WAY P  X"), so patterns accept both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# descriptor types
CARD, CARD_REFUND, MBWAY, TRANSFER, DIRECT_DEBIT, SERVICE, ATM, FEE, SALARY, TOPUP, FX, INTEREST, OTHER = (
    "card", "card_refund", "mbway", "transfer", "direct_debit", "service_payment", "atm", "fee",
    "salary", "topup", "fx", "interest", "other",
)


@dataclass(frozen=True)
class Parsed:
    type: str
    party: str  # merchant (card/DD/service) or counterparty (transfers); "" for fees etc.


_SEP = r"[ /.]*"
MILLENNIUM = [
    (re.compile(r"^COMPRA \d{4} (.+?)(?:\s+CONTACTLESS)?$"), CARD),
    (re.compile(r"^CRED\. \d{4} (.+)$"), CARD_REFUND),
    (re.compile(r"^LEV ATM \d{4} (.+)$"), ATM),
    (re.compile(rf"^TRF MB WAY (?:P{_SEP}|DE{_SEP})(.+)$"), MBWAY),
    (re.compile(r"^TRANSFERENCIA - VENCIMENTO.*$"), SALARY),
    (re.compile(rf"^TRF\.?{_SEP}P{_SEP}O?{_SEP}(.+)$"), TRANSFER),
    (re.compile(r"^TRF (.+)$"), TRANSFER),
    (re.compile(r"^DD (.+?)\s+\d{5,}.*$"), DIRECT_DEBIT),
    (re.compile(r"^PAGSERV (.+?)\s+\d{5}\s+\d+.*$"), SERVICE),
    (re.compile(r"^PAGSERV (.+)$"), SERVICE),
    (re.compile(r"^(?:COM\.|COMISSAO|CUSTO DE SERVICO|IMPOSTO (?:DO )?SELO|JUROS DEVEDORES).*$"), FEE),
]

REVOLUT_CODES = {
    "CARD_PAYMENT": CARD, "Pagamento com cartão": CARD, "Card Payment": CARD,
    "CARD_REFUND": CARD_REFUND, "Devolução do cartão de débito": CARD_REFUND, "Card Refund": CARD_REFUND,
    "TOPUP": TOPUP, "Carregamento": TOPUP, "Topup": TOPUP,
    "TRANSFER": TRANSFER, "Transferência": TRANSFER, "Transfer": TRANSFER,
    "ATM": ATM,
    "EXCHANGE": FX, "Câmbio": FX, "Exchange": FX,
    "CHARGE": FEE, "Cobrança": FEE, "Fee": FEE,
    "Juros": INTEREST, "Interest": INTEREST, "INTEREST": INTEREST,
}
_REVOLUT_PREFIX = re.compile(r"^(?:Refund from|Payment from|From|To|Sent from Revolut\s+To|Para)\s+", re.IGNORECASE)
_PENDING = re.compile(r"\s*\*?\s*Pending$", re.IGNORECASE)


def parse(description: str, bank_tx_code: str | None = None, institution: str = "") -> Parsed:
    d = " ".join((description or "").split())
    inst = institution.lower()

    if inst.startswith("millennium"):
        for rx, kind in MILLENNIUM:
            if m := rx.match(d):
                return Parsed(kind, m.group(1).strip() if m.groups() else "")
        return Parsed(OTHER, d)

    if inst.startswith("revolut"):
        kind = REVOLUT_CODES.get(bank_tx_code or "", OTHER)
        if re.match(r"^Pagamento de juros", d):
            kind = INTEREST
        party = _PENDING.sub("", _REVOLUT_PREFIX.sub("", d)).strip()
        if kind in (FX, FEE, INTEREST, TOPUP):
            party = ""
        return Parsed(kind, party)

    if inst.startswith("santander"):
        if m := re.match(r"^Ordenado de (.+)$", d):
            return Parsed(SALARY, m.group(1))
        return Parsed(CARD, d)

    return Parsed(OTHER, d)
