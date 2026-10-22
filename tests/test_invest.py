from datetime import date

import pandas as pd
import pytest

from fin.categorize import taxonomy
from fin.categorize.manual import set_category
from fin.ingest.dedupe import insert_transactions
from fin.ingest.model import RawTxn
from fin.invest import lots, portfolio, prices
from fin.pipeline import reprocess

DEGIRO_CSV = """\
Data,Hora,Produto,ISIN,Bolsa de referência,Bolsa,Quantidade,Preços,,Valor local,,Valor EUR,Taxa de Câmbio,Taxa Autofx,Custos de transação e/ou taxas de terceiros,Total EUR,ID da Ordem
05-10-2026,00:00,VANGUARD FTSE ALL-WORLD,IE00BK5BQT80,XET,,10,"171,1400",EUR,"-1711,40",EUR,"-1711,40","0,0000","0,00",,"-1711,40",
01-06-2026,16:13,VIRGIN GALACTIC,US92766K4031,NSY,CDED,-300,"8,2500",USD,"2475,00",USD,"2131,25","1,1613","-5,33","-2,00","2123,92",b854
01-06-2026,15:35,VIRGIN GALACTIC,US92766K4031,NSY,CDED,100,"7,6250",USD,"-762,50",USD,"-655,92","1,1625","-1,64",,"-657,56",5384
01-06-2026,15:35,VIRGIN GALACTIC,US92766K4031,NSY,CDED,100,"7,6250",USD,"-762,50",USD,"-655,92","1,1625","-1,64",,"-657,56",5384
01-06-2026,15:35,VIRGIN GALACTIC,US92766K4031,NSY,CDED,100,"7,6250",USD,"-762,50",USD,"-655,92","1,1625","-1,64",,"-657,56",5384
02-03-2026,09:05,VANGUARD FTSE ALL-WORLD,IE00BK5BQT80,XET,XETA,9,"148,5800",EUR,"-1337,22",EUR,"-1337,22","0,0000","0,00","-1,00","-1338,22",aa01
27-02-2026,18:13,VANGUARD FTSE ALL-WORLD,IE00BK5BQT80,TDG,XGAT,-4,"149,6400",EUR,"598,56",EUR,"598,56","0,0000","0,00","-1,00","597,56",aa02
"""


@pytest.fixture
def degiro(tmp_path):
    p = tmp_path / "Transactions.csv"
    p.write_text(DEGIRO_CSV, encoding="utf-8")
    return p


def test_degiro_parse_and_idempotent_import(con, degiro):
    assert lots.detect(degiro)
    rows = lots.parse_degiro(degiro)
    assert [r["kind"] for r in rows].count("transfer_in") == 1
    assert rows[2]["price"] == 7.625 and rows[1]["fees"] == 7.33  # 4-dp price kept; autofx + fee
    assert lots.import_degiro(con, degiro)["new"] == 7  # partial fills sharing an order id stay distinct
    assert lots.import_degiro(con, degiro) == {"rows": 7, "new": 0, "updated": 7, "locked_skipped": 0}


def test_fifo_and_transfer_supersede(con, degiro):
    lots.import_degiro(con, degiro)
    # sell of 4 VWCE on 27-02 happens before any buy in this file -> nothing to match yet; add an
    # older original lot (Millennium era) to cover it and the transfer-in
    lots.add_lot(con, trade_date="2021-03-01", isin="IE00BK5BQT80", quantity=14, price=90.0, fees=4.0)
    xfer = con.execute("SELECT id FROM lots WHERE kind='transfer_in'").fetchone()[0]
    s = portfolio.summary(con)
    assert s["has_transfer_placeholder"]
    con.execute("UPDATE lots SET superseded=1 WHERE id=?", (xfer,))
    pos = portfolio.positions(con)
    vg, vw = pos["US92766K4031"], pos["IE00BK5BQT80"]
    assert vg.quantity == 0 and vg.realised_eur == pytest.approx(2123.92 - 3 * 657.56)
    unit = (14 * 90 + 4) / 14
    assert vw.quantity == 19  # 14 original - 4 sold + 9
    assert vw.realised_eur == pytest.approx(597.56 - 4 * unit)
    assert vw.cost_eur == pytest.approx(10 * unit + 1338.22)


def test_daily_series_values_and_contributions(con, degiro):
    lots.import_degiro(con, degiro)
    fake = {"VWCE.DE": 150.0, "SPCE": 8.0, "EURUSD=X": 1.25}

    def fetch(ticker, start):
        return pd.Series({date(2026, 2, 27): fake[ticker], date(2026, 10, 5): fake[ticker]})

    prices.refresh(con, fetch=fetch)
    s = portfolio.daily_series(con, end=date(2026, 10, 6))
    last = s.iloc[-1]
    assert last["units_VWCE.DE"] == 15  # -4 + 9 + 10
    assert last["value_eur"] == pytest.approx(15 * 150.0)
    expected_contrib = 1711.40 - 2123.92 + 3 * 657.56 + 1338.22 - 597.56
    assert last["contributed_eur"] == pytest.approx(expected_contrib)
    # USD position valued through EURUSD on a day it was held (none: bought and sold same day)
    assert s.loc["2026-06-01", "units_SPCE"] == 0


def test_manual_category_tag_and_rule(con):
    con.execute("INSERT INTO accounts (id, key, name, institution) VALUES (1, 'k', 'Main', 'Millennium BCP')")
    insert_transactions(con, 1, "eb", [RawTxn(date(2026, 7, 1), 18_000_000, "EUR", "M DA SILVA PEREIRA"),
                                       RawTxn(date(2026, 7, 2), -999, "EUR", "COMPRA 3043 NEW SHOP LISBOA")])
    reprocess(con)
    gift, shop = [r[0] for r in con.execute("SELECT id FROM transactions ORDER BY id")]
    set_category(con, gift, "Gifts received", tags=("one-off",), note="family")
    rule = set_category(con, shop, "Shopping > Other shopping", make_rule=True)
    insert_transactions(con, 1, "eb", [RawTxn(date(2026, 8, 1), -500, "EUR", "COMPRA 3043 NEW SHOP AMADORA")])
    st = reprocess(con)
    assert rule and st.manual == 2 and st.rule == 1
    paths = {v: k for k, v in taxonomy.path_ids(con).items()}
    cats = [paths.get(r[0]) for r in con.execute("SELECT category_id FROM tx_enriched ORDER BY tx_id")]
    assert cats == ["Gifts received", "Shopping > Other shopping", "Shopping > Other shopping"]
    with pytest.raises(ValueError):
        set_category(con, gift, "Nope")
