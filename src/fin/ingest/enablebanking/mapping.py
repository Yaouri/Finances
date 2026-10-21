"""Enable Banking JSON -> RawTxn / balance rows."""

from __future__ import annotations

from datetime import date

from ..model import RawTxn, to_cents


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def map_transaction(t: dict) -> RawTxn | None:
    """Returns None for non-booked (pending) transactions — those aren't stored."""
    if t.get("status") not in (None, "BOOK"):
        return None
    amt = t["transaction_amount"]
    cents = abs(to_cents(amt["amount"]))
    outflow = t.get("credit_debit_indicator") == "DBIT"
    cents = -cents if outflow else cents

    party = (t.get("creditor") if outflow else t.get("debtor")) or {}
    party_acct = (t.get("creditor_account") if outflow else t.get("debtor_account")) or {}
    remit = [r for r in (t.get("remittance_information") or []) if r and r.strip()]
    btc = t.get("bank_transaction_code") or {}
    description = " ".join(remit) or t.get("note") or btc.get("description") or party.get("name") or ""

    orig_cents = orig_ccy = None
    instructed = (t.get("exchange_rate") or {}).get("instructed_amount")
    if instructed and instructed.get("currency") and instructed["currency"] != amt["currency"]:
        orig_cents = abs(to_cents(instructed["amount"])) * (-1 if outflow else 1)
        orig_ccy = instructed["currency"]

    bal = t.get("balance_after_transaction")
    booking = _d(t.get("booking_date")) or _d(t.get("value_date")) or _d(t.get("transaction_date"))
    if booking is None:
        return None

    return RawTxn(
        booking_date=booking,
        value_date=_d(t.get("value_date")),
        amount_cents=cents,
        currency=amt["currency"],
        description=description.strip(),
        external_id=t.get("transaction_id") or t.get("entry_reference") or None,
        counterparty_name=party.get("name"),
        counterparty_iban=(party_acct.get("iban") or None),
        bank_tx_code=btc.get("code") or btc.get("description"),
        mcc=t.get("merchant_category_code"),
        orig_amount_cents=orig_cents,
        orig_currency=orig_ccy,
        balance_after_cents=to_cents(bal["amount"]) if bal and bal.get("amount") is not None else None,
        raw=t,
    )


def map_balances(payload: dict, today: date) -> list[dict]:
    out = []
    for b in payload.get("balances", []):
        amt = b.get("balance_amount") or {}
        if amt.get("amount") is None:
            continue
        as_of = _d(b.get("reference_date")) or _d(b.get("last_change_date_time")) or today
        out.append(
            {
                "as_of": as_of.isoformat(),
                "balance_type": b.get("balance_type") or "OTHR",
                "amount_cents": to_cents(amt["amount"]),
                "currency": amt.get("currency") or "EUR",
            }
        )
    return out


# Preference order when one number per account is needed.
BALANCE_PREFERENCE = ["CLBD", "ITBD", "XPCD", "ITAV", "CLAV", "OPBD", "INFO", "OTHR"]
