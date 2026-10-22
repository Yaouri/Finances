from datetime import date

import pytest

from fin.categorize import rules as R
from fin.categorize import taxonomy
from fin.ingest.dedupe import insert_transactions
from fin.ingest.model import RawTxn
from fin.normalize import descriptors as D
from fin.normalize.merchants import MerchantResolver, brand_for, clean_key
from fin.pipeline import reprocess
from fin.transfers import Leg, detect


@pytest.mark.parametrize("desc,code,inst,expected", [
    ("COMPRA 3043 PINGO DOCE LISBOA PT      CONTACTLESS", None, "Millennium BCP", (D.CARD, "PINGO DOCE LISBOA PT")),
    ("CRED. 3043 ZARA LISBOA", None, "Millennium BCP", (D.CARD_REFUND, "ZARA LISBOA")),
    ("LEV ATM 3043 CPV CABO VERDE PRAIA", None, "Millennium BCP", (D.ATM, "CPV CABO VERDE PRAIA")),
    ("TRF MB WAY P/ JOANA COSTA", None, "Millennium BCP", (D.MBWAY, "JOANA COSTA")),
    ("TRF MB WAY P  JOANA COSTA", None, "Millennium BCP", (D.MBWAY, "JOANA COSTA")),  # API form
    ("TRF. P/O IOLANDA BARBOSA", None, "Millennium BCP", (D.TRANSFER, "IOLANDA BARBOSA")),
    ("TRF. P O IOLANDA BARBOSA", None, "Millennium BCP", (D.TRANSFER, "IOLANDA BARBOSA")),
    ("TRF P  DEGIRO", None, "Millennium BCP", (D.TRANSFER, "DEGIRO")),
    ("TRANSFERENCIA - VENCIMENTO", None, "Millennium BCP", (D.SALARY, "")),
    ("DD JJP GEST GINAS 00010183818    PT52101912", None, "Millennium BCP", (D.DIRECT_DEBIT, "JJP GEST GINAS")),
    ("PAGSERV EMEL easypa 11843 644215868 EASYPAY INS", None, "Millennium BCP", (D.SERVICE, "EMEL easypa")),
    ("IMPOSTO SELO ART 17.3.4", None, "Millennium BCP", (D.FEE, "")),
    ("COM.MAN.CONTA PACOTE CLIENTE FREQUENTE   092026", None, "Millennium BCP", (D.FEE, "")),
    ("Refund from Zara Home", "CARD_REFUND", "Revolut", (D.CARD_REFUND, "Zara Home")),
    ("Uber   * Eats Pending", "CARD_PAYMENT", "Revolut", (D.CARD, "Uber * Eats")),
    ("Exchanged to MAD", "EXCHANGE", "Revolut", (D.FX, "")),
    ('Pagamento de juros líquidos para subconta "Instant Access Savings"', "Juros", "Revolut", (D.INTEREST, "")),
    ("Ordenado de ACME SA", None, "Santander", (D.SALARY, "ACME SA")),
])
def test_descriptors(desc, code, inst, expected):
    p = D.parse(desc, code, inst)
    assert (p.type, p.party) == expected


@pytest.mark.parametrize("raw,key,brand", [
    ("PINGO DOCE LISBOA 123", "PINGO DOCE", "Pingo Doce"),
    ("Pingo Doce Amadora", "PINGO DOCE", "Pingo Doce"),
    ("Continente Bom Diaoi", "CONTINENTE BOM DIAOI", "Continente"),
    ("Glovo 01SEP LIX1TYVQ3 LISBOA PT", "GLOVO", "Glovo"),
    ("Bk32777-almirante Re", "BK ALMIRANTE RE", "Burger King"),
    ("Vifra - Mcdonald Sle", "VIFRA MCDONALD SLE", "McDonald's"),
    ("CA TorBox 515418 USD TC   0.8766666", "CA TORBOX", "Torbox"),
    ("Metropolitano de Lisboa CONTACTLESS", "METROPOLITANO", "Metro de Lisboa"),
    ("A1 SANTA EUFEMIA", "A1 SANTA EUFEMIA", None),
])
def test_merchant_clean_and_brand(raw, key, brand):
    assert clean_key(raw) == key
    assert brand_for(key) == brand


def test_merchant_resolver_clusters_truncations(con):
    r = MerchantResolver(con)
    _, a, name = r.resolve("Centro Comercial Amo")
    _, b, _ = r.resolve("CENTRO COMERCIAL AMOLIS")
    _, c, _ = r.resolve("Nos Cinema Amoreiras")
    assert a == b and name == "Centro Comercial Amo"
    assert c != a


def _leg(i, acc, d, cents, typ, party="", merchant=None, iban=None, joint=False):
    return Leg(i, acc, "joint" if joint else "current", joint, date.fromisoformat(d), None, cents, typ, party,
               merchant, iban)


def test_transfer_pairing_and_singles():
    legs = [
        _leg(1, 4, "2026-09-03", -100000, D.CARD, "REVOLUT DUBLIN IE", merchant="Revolut"),  # Millennium top-up
        _leg(2, 1, "2026-09-01", 100000, D.TOPUP),                                            # Revolut side
        _leg(3, 1, "2026-09-20", -500000, D.TRANSFER, "INSTANT ACCESS SAVINGS"),
        _leg(4, 3, "2026-09-20", 500000, D.TRANSFER, "POUPANCA DE ACESSO IMEDIATO"),
        _leg(5, 4, "2026-07-29", -977500, D.TRANSFER, "DYLAN PEREIRA"),                       # twin untracked
        _leg(6, 2, "2026-09-20", 1900, D.TRANSFER, "IOLANDA PATRICIA F", joint=True),
        _leg(7, 4, "2026-08-26", -900000, D.TRANSFER, "DEGIRO"),
        _leg(8, 4, "2026-05-04", 174354, D.TRANSFER, "TRADING 212 UK"),
        _leg(9, 4, "2026-09-05", -1000, D.CARD, "PINGO DOCE", merchant="Pingo Doce"),
        _leg(10, 2, "2026-09-20", 50000, D.TRANSFER, "DYLAN P", joint=True),
        _leg(11, 4, "2026-09-25", -50000, D.MBWAY, "SOMEONE ELSE"),  # same amount, but a different person
    ]
    m = detect(legs, own_ibans=set(), owner_names={"Dylan Pereira"})
    assert m[1].group == m[2].group and m[1].category == "Internal transfer"
    assert m[3].group == m[4].group
    assert m[5].category == "Internal transfer" and m[5].group.startswith("u")
    assert m[6].category == "Partner contribution"
    assert (m[7].category, m[8].category) == ("Brokerage deposit", "Brokerage withdrawal")
    assert 9 not in m
    # +500 from "Dylan P" pairs with nothing out of an own account within the window -> owner single leg
    assert m[10].category == "Internal transfer" and 11 not in m


def test_rules_first_match_and_constraints(con):
    taxonomy.seed(con)
    ids = taxonomy.path_ids(con)
    R.add_rule(con, "merchant", "^Pingo Doce$", ids["Food > Groceries"])
    R.add_rule(con, "type", "^transfer$", ids["Refunds & reimbursements"], sign=1, max_cents=100000)
    rules = R.load(con)
    ctx = {"merchant": "Pingo Doce", "type": "card", "amount_cents": -500, "account_id": 1, "account_kind": "current"}
    assert R.first_match(rules, ctx).category_id == ids["Food > Groceries"]
    big = {**ctx, "merchant": "", "type": "transfer", "amount_cents": 18_000_000}
    assert R.first_match(rules, big) is None
    with pytest.raises(ValueError):
        R.add_rule(con, "nope", "x", 1)


def test_pipeline_end_to_end(con):
    con.execute("INSERT INTO accounts (id, key, name, institution) VALUES (1, 'iban:PT1', 'Main', 'Millennium BCP')")
    con.execute("INSERT INTO accounts (id, key, name, institution, kind, is_joint, my_share) "
                "VALUES (2, 'iban:PT2', 'Joint', 'Revolut', 'joint', 1, 0.5)")
    insert_transactions(con, 1, "eb", [
        RawTxn(date(2026, 9, 1), -1060, "EUR", "COMPRA 3043 PINGO DOCE LISBOA PT CONTACTLESS"),
        RawTxn(date(2026, 9, 24), 209357, "EUR", "TRANSFERENCIA - VENCIMENTO"),
        RawTxn(date(2026, 9, 3), -24, "EUR", "IMPOSTO SELO ART 17.3.4"),
        RawTxn(date(2026, 9, 9), -12345, "EUR", "COMPRA 3043 TOTALLY UNKNOWN THING"),
    ])
    insert_transactions(con, 2, "eb", [RawTxn(date(2026, 9, 2), -2000, "EUR", "Lidl Casal Ribeiro",
                                              bank_tx_code="CARD_PAYMENT")])
    st = reprocess(con)
    assert (st.total, st.rule, st.uncategorised) == (5, 4, 1)
    rows = {r["description"][:20]: r for r in con.execute(
        """SELECT t.description, c.path, e.flow, e.my_amount_eur_cents FROM transactions t
           JOIN tx_enriched e ON e.tx_id = t.id LEFT JOIN categories c ON c.id = e.category_id""")}
    assert rows["COMPRA 3043 PINGO DO"]["path"] == "Food > Groceries"
    assert rows["COMPRA 3043 TOTALLY "]["path"] is None
    assert rows["TRANSFERENCIA - VENC"]["path"] == "Salary" and rows["TRANSFERENCIA - VENC"]["flow"] == "income"
    assert rows["IMPOSTO SELO ART 17."]["path"] == "Fees & taxes > Stamp duty"
    assert rows["Lidl Casal Ribeiro"]["my_amount_eur_cents"] == -1000  # joint: my 50% share

    # manual override wins and survives reprocessing
    tx_id = con.execute("SELECT id FROM transactions WHERE description LIKE '%UNKNOWN%'").fetchone()[0]
    cat = taxonomy.path_ids(con)["Shopping > Other shopping"]
    con.execute("INSERT INTO tx_user (tx_id, category_id) VALUES (?, ?)", (tx_id, cat))
    st = reprocess(con)
    assert (st.manual, st.uncategorised) == (1, 0)
