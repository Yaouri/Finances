from datetime import date

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fin.ingest.dedupe import insert_transactions
from fin.ingest.enablebanking.auth import make_jwt
from fin.ingest.enablebanking.consent import ConsentError, parse_callback
from fin.ingest.enablebanking.mapping import map_balances, map_transaction
from fin.ingest.model import RawTxn, assign_fingerprints, norm_text, to_cents


@pytest.mark.parametrize(
    "s,expected",
    [("12.30", 1230), ("-12,30", -1230), ("1.234,56", 123456), ("1,234.56", 123456),
     ("€ 5", 500), ("0.005", 1), (12.3, 1230), ("1 000,00", 100000)],
)
def test_to_cents(s, expected):
    assert to_cents(s) == expected


def test_norm_text_strips_accents_and_noise():
    assert norm_text("Compra  Café São João, nº3") == "COMPRA CAFE SAO JOAO NO3"


def test_fingerprint_repeats_are_distinct_and_stable():
    a = [RawTxn(date(2026, 1, 5), -70, "EUR", "COMPRA CAFE") for _ in range(2)]
    b = [RawTxn(date(2026, 1, 5), -70, "EUR", "compra café") for _ in range(2)]
    fa = [t.fingerprint for t in assign_fingerprints(a)]
    fb = [t.fingerprint for t in assign_fingerprints(b)]
    assert fa[0] != fa[1]
    assert fa == fb


def test_jwt_claims_and_header():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    tok = make_jwt("app-123", pem, ttl=600, now=1_700_000_000)
    assert jwt.get_unverified_header(tok) == {"alg": "RS256", "kid": "app-123", "typ": "JWT"}
    claims = jwt.decode(tok, key.public_key(), algorithms=["RS256"], audience="api.enablebanking.com",
                        options={"verify_exp": False})
    assert claims == {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
                      "iat": 1_700_000_000, "exp": 1_700_000_600}
    with pytest.raises(ValueError):
        make_jwt("x", pem, ttl=86_401)


EB_DEBIT = {
    "transaction_id": "tx-1",
    "transaction_amount": {"currency": "EUR", "amount": "23.45"},
    "credit_debit_indicator": "DBIT",
    "status": "BOOK",
    "booking_date": "2026-03-02",
    "value_date": "2026-03-01",
    "creditor": {"name": "PINGO DOCE"},
    "creditor_account": {"iban": "PT50999"},
    "remittance_information": ["COMPRA 1234 PINGO DOCE LISBOA", ""],
    "bank_transaction_code": {"description": "Card payment", "code": "PMNT"},
    "exchange_rate": {"instructed_amount": {"currency": "USD", "amount": "25.00"}},
}


def test_map_debit():
    t = map_transaction(EB_DEBIT)
    assert t.amount_cents == -2345
    assert t.description == "COMPRA 1234 PINGO DOCE LISBOA"
    assert t.counterparty_name == "PINGO DOCE" and t.counterparty_iban == "PT50999"
    assert t.external_id == "tx-1"
    assert (t.orig_amount_cents, t.orig_currency) == (-2500, "USD")
    assert t.booking_date == date(2026, 3, 2) and t.value_date == date(2026, 3, 1)


def test_map_credit_uses_debtor_and_entry_reference():
    t = map_transaction({
        "entry_reference": "er-9", "transaction_amount": {"currency": "EUR", "amount": "2000.00"},
        "credit_debit_indicator": "CRDT", "status": "BOOK", "booking_date": "2026-03-25",
        "debtor": {"name": "ACME LDA"}, "remittance_information": ["TRF SALARIO MARCO"],
    })
    assert t.amount_cents == 200000 and t.counterparty_name == "ACME LDA" and t.external_id == "er-9"


def test_map_skips_pending():
    assert map_transaction({**EB_DEBIT, "status": "PDNG"}) is None


def test_map_balances():
    rows = map_balances({"balances": [
        {"balance_amount": {"currency": "EUR", "amount": "1500.10"}, "balance_type": "CLBD", "reference_date": "2026-03-31"},
        {"balance_amount": {"currency": "EUR", "amount": None}, "balance_type": "ITAV"},
    ]}, date(2026, 4, 1))
    assert rows == [{"as_of": "2026-03-31", "balance_type": "CLBD", "amount_cents": 150010, "currency": "EUR"}]


def test_parse_callback():
    assert parse_callback("https://localhost:8501/callback?state=abc&code=xyz") == ("xyz", "abc")
    assert parse_callback("  xyz ") == ("xyz", None)
    with pytest.raises(ConsentError):
        parse_callback("https://localhost:8501/callback?error=access_denied&state=abc")


def _t(d, cents, desc, **kw):
    return RawTxn(date.fromisoformat(d), cents, "EUR", desc, **kw)


def test_insert_is_idempotent(con, account):
    batch = lambda: [_t("2026-01-02", -500, "A", external_id="1"), _t("2026-01-02", -500, "A", external_id="2"),
                     _t("2026-01-03", -900, "B")]
    s1 = insert_transactions(con, account, "eb", batch())
    s2 = insert_transactions(con, account, "eb", batch())
    assert (s1.new, s2.new, s2.existing) == (3, 0, 3)


def test_cross_source_csv_vs_api(con, account):
    api = [_t("2026-01-02", -2345, "COMPRA 1234 PINGO DOCE LISBOA", external_id="x1"),
           _t("2026-01-02", -70, "CAFE"), _t("2026-01-02", -70, "CAFE")]
    insert_transactions(con, account, "eb", api)
    # CSV: dates shifted by a day (value date), different wording, plus one row the API lacks
    csv = [_t("2026-01-01", -2345, "PINGO DOCE LISBOA"), _t("2026-01-02", -70, "Compra Cafe"),
           _t("2026-01-02", -70, "Compra Cafe"), _t("2025-10-01", -1000, "OLD")]
    st = insert_transactions(con, account, "csv:millennium", csv)
    assert (st.new, st.cross_source_dupes) == (1, 3)
    live = con.execute("SELECT count(*) FROM transactions WHERE duplicate_of IS NULL").fetchone()[0]
    assert live == 4
    # each API row is claimed at most once
    claimed = con.execute("SELECT count(DISTINCT duplicate_of) FROM transactions WHERE duplicate_of IS NOT NULL").fetchone()[0]
    assert claimed == 3


def test_cross_source_requires_same_amount_and_window(con, account):
    insert_transactions(con, account, "eb", [_t("2026-01-10", -1000, "X")])
    st = insert_transactions(con, account, "csv:revolut", [_t("2026-01-10", -1001, "X"), _t("2026-01-20", -1000, "X")])
    assert st.cross_source_dupes == 0 and st.new == 2


class _FakeEB:
    def __init__(self, accounts):
        self.accounts = accounts

    def create_session(self, code):
        return {"session_id": f"s-{code}", "access": {"valid_until": "2027-01-01T00:00:00+00:00"},
                "aspsp": {"name": "Revolut", "country": "PT"}, "accounts": self.accounts}


def _pending(con, state, bank):
    con.execute("INSERT INTO eb_pending_auth (state, aspsp_name, aspsp_country, valid_until, auth_url) "
                "VALUES (?, ?, 'PT', '2027-01-01T00:00:00+00:00', 'u')", (state, bank))


def test_finish_upserts_by_iban_and_detects_joint(con):
    from fin.ingest.enablebanking.consent import finish

    accs = [{"uid": "u1", "account_id": {"iban": "PT50 1"}, "identification_hash": "h1", "name": "Me", "currency": "EUR",
             "cash_account_type": "CACC"},
            {"uid": "u2", "account_id": {"iban": "PT502"}, "identification_hash": "h2", "name": "ME & YOU",
             "currency": "EUR", "cash_account_type": "CACC"}]
    _pending(con, "st1", "Revolut")
    finish(con, _FakeEB(accs), "https://localhost/cb?code=c1&state=st1")
    con.execute("UPDATE accounts SET name='Renamed' WHERE key='iban:PT501'")
    # re-consent: new uids, same IBANs -> same rows, user edits kept
    accs2 = [{**a, "uid": a["uid"] + "b"} for a in accs]
    _pending(con, "st2", "Revolut")
    finish(con, _FakeEB(accs2), "https://localhost/cb?code=c2&state=st2")
    rows = con.execute("SELECT key, name, kind, is_joint, my_share, eb_uid FROM accounts ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("iban:PT501", "Renamed", "current", 0, 1.0, "u1b"),
                                        ("iban:PT502", "ME & YOU", "joint", 1, 0.5, "u2b")]
    assert con.execute("SELECT count(*) FROM eb_sessions WHERE revoked_at IS NULL").fetchone()[0] == 1


def test_finish_bare_code_needs_unambiguous_bank(con):
    from fin.ingest.enablebanking.consent import finish

    _pending(con, "a", "Millennium BCP")
    _pending(con, "b", "Santander Totta")
    with pytest.raises(ConsentError):
        finish(con, _FakeEB([]), "somecode")
    finish(con, _FakeEB([]), "somecode", bank="millennium")
    assert [r[0] for r in con.execute("SELECT aspsp_name FROM eb_pending_auth")] == ["Santander Totta"]


def test_exact_match_claims_before_fuzzy(con, account):
    # API only has the 04-22 canteen charge; the PDF has 04-21 and 04-22 (same amount)
    insert_transactions(con, account, "eb", [_t("2026-04-22", -640, "COMPRA 3043 GERTAL", external_id="e1")])
    st = insert_transactions(con, account, "pdf:millennium",
                             [_t("2026-04-21", -640, "COMPRA 3043 GERTAL"), _t("2026-04-22", -640, "COMPRA 3043 GERTAL")])
    assert (st.existing, st.new, st.cross_source_dupes) == (1, 1, 0)
