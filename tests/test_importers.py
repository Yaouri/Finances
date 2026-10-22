from datetime import date

import pandas as pd
import pytest

from fin.ingest import importers
from fin.ingest.dedupe import insert_transactions
from fin.ingest.importers import millennium_pdf, revolut, santander_meal
from fin.ingest.importers.base import ImportFormatError
from fin.ingest.model import RawTxn

MILLENNIUM_LINES = """\
EXTRATO COMBINADO
IBAN: PT50 0033 0000 1111 2222 3333 4
BIC/SWIFT: BCOMPTPL
EXTRATO DE 2025/12/01 A 2026/01/31
SALDO INICIAL 1 000.00
12.30 12.30 COMPRA 3043 PINGO DOCE LISBOA PT CONTACTLESS 10.60 989.40
12.31 12.31 PAGSERV EMEL 123 456.00 533.40
A TRANSPORTAR 533.40
TRANSPORTE 533.40
1.02 1.01 TRANSFERENCIA - VENCIMENTO 2 093.57 2 626.97
1.03 1.03 IMPOSTO DO SELO 0.19 2 626.78
SALDO FINAL 2 626.78
CONTA TITULOS N: 1
VANG FT ALL WLD 282.0000 EUR 169.42 0 0 47 776.44
(ISIN: IE00BK5BQT80)
""".splitlines()


def test_millennium_pdf_lines():
    res = millennium_pdf.parse_lines(MILLENNIUM_LINES)
    (b,) = res.batches
    assert b.iban == "PT50003300001111222233334"
    got = [(t.booking_date, t.value_date, t.amount_cents, t.description) for t in b.txns]
    assert got == [
        (date(2025, 12, 30), date(2025, 12, 30), -1060, "COMPRA 3043 PINGO DOCE LISBOA PT CONTACTLESS"),
        # description ends in digit groups: the amount is resolved through the balance delta
        (date(2025, 12, 31), date(2025, 12, 31), -45600, "PAGSERV EMEL 123"),
        (date(2026, 1, 2), date(2026, 1, 1), 209357, "TRANSFERENCIA - VENCIMENTO"),
        (date(2026, 1, 3), date(2026, 1, 3), -19, "IMPOSTO DO SELO"),
    ]
    assert b.balances == [(date(2026, 1, 31), 262678, "EUR")]
    assert b.extra["holdings"][0] | {} == {
        "name": "VANG FT ALL WLD", "isin": "IE00BK5BQT80", "quantity": 282.0, "currency": "EUR",
        "price": 169.42, "value_cents": 4777644, "as_of": "2026-01-31"}


def test_millennium_pdf_must_reconcile():
    bad = [ln.replace("SALDO FINAL 2 626.78", "SALDO FINAL 2 626.79") for ln in MILLENNIUM_LINES]
    with pytest.raises(ImportFormatError):
        millennium_pdf.parse_lines(bad)


REVOLUT_CSV = """\
Tipo,Produto,Data de início,Data de Conclusão,Descrição,Montante,Comissão,Moeda,Estado,Saldo
Pagamento com cartão,Atual,2026-09-05 16:03:01,2026-09-06 10:47:56,Guest House,-45.19,0.90,EUR,CONCLUÍDA,848.14
Cobrança,Atual,2026-08-10 19:02:08,2026-08-10 19:02:08,Comissão de entrega de cartão,0.00,6.99,EUR,CONCLUÍDA,0.99
Pagamento com cartão,Atual,2026-09-06 10:00:00,,Pending thing,-5.00,0.00,EUR,PENDENTE,
Transferência,Depósito,2026-09-20 19:19:27,2026-09-20 19:19:28,Para Poupança de Acesso Imediato,5000.00,0.00,EUR,CONCLUÍDA,5000.00
Juros,Depósito,2026-09-21 03:26:55,2026-09-21 03:26:55,Pagamento de juros líquidos,0.20,0.05,EUR,CONCLUÍDA,5000.15
"""


def test_revolut_csv(tmp_path):
    p = tmp_path / "account-statement.csv"
    p.write_text(REVOLUT_CSV, encoding="utf-8")
    assert revolut.detect(p)
    res = revolut.parse(p)
    main, sav = res.batches
    assert [(t.booking_date, t.amount_cents) for t in main.txns] == [(date(2026, 8, 10), -699), (date(2026, 9, 5), -4609)]
    assert main.txns[1].value_date == date(2026, 9, 6)
    assert [t.amount_cents for t in sav.txns] == [500000, 15]
    assert sav.balances == [(date(2026, 9, 21), 500015, "EUR")]


def test_santander_meal_xlsx(tmp_path):
    rows = [["Quinta-feira, 22 de Outubro de 2026", None, None, None], [None] * 4,
            ["Consultar Movimentos de Cartão", None, None, None], ["Nº Cartão XXXXXXXXXXXX1234", None, None, None],
            [None] * 4, ["Listagem Movimentos", None, None, None],
            ["Data Operação", "Data Valor", "Descrição", "Montante"],
            ["22-10-2026", "22-10-2026", "Gertal Carnaxide", "-6,40 EUR"],
            ["24-09-2026", "24-09-2026", "Ordenado de ACME", "292,22 EUR"]]
    p = tmp_path / "descarga.xlsx"
    pd.DataFrame(rows).to_excel(p, header=False, index=False)
    assert santander_meal.detect(p)
    (b,) = santander_meal.parse(p).batches
    assert [(t.booking_date, t.amount_cents, t.description) for t in b.txns] == [
        (date(2026, 9, 24), 29222, "Ordenado de ACME"), (date(2026, 10, 22), -640, "Gertal Carnaxide")]


def test_import_routes_revolut_by_overlap_and_is_idempotent(con, tmp_path):
    for i, name in ((1, "Personal"), (2, "Joint")):
        con.execute("INSERT INTO accounts (id, key, name, institution, kind) VALUES (?,?,?, 'Revolut', ?)",
                    (i, f"k{i}", name, "joint" if i == 2 else "current"))
    insert_transactions(con, 1, "eb", [RawTxn(date(2026, 9, 5), -4609, "EUR", "Guest House", external_id="a"),
                                       RawTxn(date(2026, 8, 10), -699, "EUR", "Card delivery fee", external_id="b")])
    p = tmp_path / "account-statement.csv"
    p.write_text(REVOLUT_CSV, encoding="utf-8")
    out = importers.import_file(con, p)
    # identical text -> fingerprint hit ("existing"); different wording -> cross-source match
    assert [(o.account, o.new, o.existing, o.duplicates) for o in out] == [
        ("Personal", 0, 1, 1), ("Revolut Savings", 2, 0, 0)]
    out2 = importers.import_file(con, p)
    assert [(o.new, o.duplicates) for o in out2] == [(0, 0), (0, 0)]
