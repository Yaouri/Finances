from datetime import date, timedelta

import pandas as pd
import pytest

from fin import balances, metrics
from fin.categorize import ml
from fin.ingest.dedupe import insert_transactions
from fin.ingest.model import RawTxn
from fin.insights import alerts, recurring
from fin.pipeline import reprocess
from fin.quality import balance_gaps


def _frame(rows):
    df = pd.DataFrame(rows, columns=["date", "merchant", "amount"])
    df["date"] = pd.to_datetime(df.date)
    df["account"] = "Main"
    df["flow"] = "expense"
    df["descriptor_type"] = "card"
    df["category"] = "Subscriptions > Streaming"
    return df


def test_recurring_monthly_with_price_change_and_clusters():
    rows = [(f"2026-{m:02d}-05", "Netflix", -12.99) for m in range(1, 7)]
    rows[-1] = ("2026-06-05", "Netflix", -13.99)
    rows += [(f"2026-{m:02d}-20", "Apple", -0.99) for m in range(1, 6)]
    rows += [(f"2026-{m:02d}-21", "Apple", -7.49) for m in range(1, 6)]
    rows += [("2026-01-03", "Shop", -20), ("2026-02-19", "Shop", -21), ("2026-02-22", "Shop", -20)]  # irregular
    s = {(x.merchant, round(x.typical, 2)): x for x in recurring.detect(_frame(rows), today=date(2026, 6, 10))}
    assert set(s) == {("Netflix", 12.99), ("Apple", 0.99), ("Apple", 7.49)}
    nf = s[("Netflix", 12.99)]
    assert nf.period == "monthly" and nf.next_expected == date(2026, 7, 5) and nf.active
    assert nf.price_change == pytest.approx(1.0)
    assert s[("Apple", 0.99)].price_change is None


def _setup_account(con, joint=False):
    con.execute("INSERT INTO accounts (id, key, name, institution, kind, is_joint, my_share) VALUES (?,?,?,?,?,?,?)",
                (1, "k1", "Main", "Millennium BCP", "joint" if joint else "current", int(joint), 0.5 if joint else 1))


def test_balance_series_and_gap_detection(con):
    _setup_account(con)
    insert_transactions(con, 1, "eb", [RawTxn(date(2026, 1, 10), -1000, "EUR", "A"),
                                       RawTxn(date(2026, 2, 10), -500, "EUR", "B"),
                                       RawTxn(date(2026, 3, 10), 2000, "EUR", "C")])
    for d, c in (("2026-01-31", 9000), ("2026-02-28", 8500), ("2026-03-31", 9999)):  # March off by -501
        con.execute("INSERT INTO balances (account_id, as_of, balance_type, amount_cents, currency) "
                    "VALUES (1, ?, 'STATEMENT', ?, 'EUR')", (d, c))
    gaps = balance_gaps(con)
    assert [(g.from_date, g.to_date, g.delta_cents) for g in gaps] == [("2026-02-28", "2026-03-31", -501)]
    s = balances.account_series(con, 1, end=date(2026, 4, 1))
    assert s.loc["2026-03-31"] == 99.99
    # history before the gap is not reconstructed (would be wrong)
    assert s.index.min() == pd.Timestamp("2026-03-09")


def test_alerts_large_duplicate_and_idempotent_status(con):
    _setup_account(con)
    today = date(2026, 6, 30)
    txns = [RawTxn(date(2026, 1, 5) + timedelta(days=7 * i), -1000, "EUR", "COMPRA 3043 CAFE SOL LISBOA") for i in range(20)]
    txns += [RawTxn(date(2026, 6, 20), -40000, "EUR", "COMPRA 3043 BIG TV STORE"),
             RawTxn(date(2026, 6, 21), -2500, "EUR", "COMPRA 3043 RESTAURANTE X"),
             RawTxn(date(2026, 6, 22), -2500, "EUR", "COMPRA 3043 RESTAURANTE X")]
    insert_transactions(con, 1, "eb", txns)
    reprocess(con)
    assert alerts.generate(con, today=today) > 0
    kinds = {r[0] for r in con.execute("SELECT kind FROM alerts")}
    assert {"large", "duplicate", "new_merchant"} <= kinds
    ids = [r[0] for r in con.execute("SELECT id FROM alerts")]
    alerts.set_status(con, ids, "ok")
    assert alerts.generate(con, today=today) == 0
    assert con.execute("SELECT count(*) FROM alerts WHERE status <> 'ok'").fetchone()[0] == 0


def test_metrics_joint_modes_and_one_off(con):
    _setup_account(con, joint=True)
    con.execute("INSERT INTO accounts (id, key, name, institution) VALUES (2, 'k2', 'Mine', 'Millennium BCP')")
    insert_transactions(con, 1, "eb", [RawTxn(date(2026, 5, 2), -10000, "EUR", "Lidl", bank_tx_code="CARD_PAYMENT")])
    insert_transactions(con, 2, "eb", [RawTxn(date(2026, 5, 3), 200000, "EUR", "TRANSFERENCIA - VENCIMENTO"),
                                       RawTxn(date(2026, 5, 4), 5000000, "EUR", "GIFT FROM FAMILY")])
    reprocess(con)
    from fin.categorize.manual import set_category
    gift = con.execute("SELECT id FROM transactions WHERE description LIKE 'GIFT%'").fetchone()[0]
    set_category(con, gift, "Gifts received", tags=("one-off",))
    reprocess(con)
    f = metrics.Filters(start=date(2026, 5, 1), end=date(2026, 5, 31))
    tot = metrics.period_totals(metrics.transactions(con, f))
    assert (tot["income"], tot["expenses"]) == (2000.0, 50.0)          # my 50% share, gift excluded
    full = metrics.period_totals(metrics.transactions(con, metrics.Filters(f.start, f.end, joint="full")))
    excl = metrics.period_totals(metrics.transactions(con, metrics.Filters(f.start, f.end, joint="exclude")))
    assert full["expenses"] == 100.0 and excl["expenses"] == 0
    assert metrics.monthly_cashflow(metrics.transactions(con, f)).one_off.iloc[0] == 50000.0


def test_ml_learns_text_patterns():
    rng = range(30)
    lab = pd.DataFrame(
        [{"merchant": f"Restaurante {i}", "description": f"COMPRA RESTAURANTE {i}", "type": "card",
          "account_kind": "current", "amount_cents": -2500, "category_id": 1, "source": "rule"} for i in rng]
        + [{"merchant": f"Farmacia {i}", "description": f"COMPRA FARMACIA {i}", "type": "card",
            "account_kind": "current", "amount_cents": -900, "category_id": 2, "source": "rule"} for i in rng])
    new = pd.DataFrame([{"merchant": "Restaurante Novo", "description": "COMPRA RESTAURANTE NOVO", "type": "card",
                         "account_kind": "current", "amount_cents": -3000},
                        {"merchant": "Farmacia Central", "description": "COMPRA FARMACIA CENTRAL", "type": "card",
                         "account_kind": "current", "amount_cents": -1200}], index=[100, 101])
    p = ml.train_and_predict(lab, new)
    assert p[100].category_id == 1 and p[101].category_id == 2
    assert min(p[100].confidence, p[101].confidence) > 0.5
