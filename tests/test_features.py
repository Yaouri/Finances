from datetime import date

import numpy as np
import pandas as pd
import pytest

from fin import metrics, report
from fin import projections as P
from fin.ingest.dedupe import insert_transactions
from fin.ingest.model import RawTxn
from fin.insights import alerts, recurring, trips
from fin.pipeline import reprocess


def _acc(con):
    con.execute("INSERT INTO accounts (id, key, name, institution) VALUES (1, 'k1', 'Main', 'Millennium BCP')")


def _netflix(con, months, amount=-1299):
    insert_transactions(con, 1, "eb", [RawTxn(date(2026, m, 5), amount, "EUR", "COMPRA 3043 NETFLIX.COM")
                                       for m in months])


def test_cancel_recurring_hides_and_alerts_on_later_charge(con):
    _acc(con)
    _netflix(con, range(1, 6))
    reprocess(con)
    df = metrics.transactions(con)
    df["amount"] = df.my_amount_eur_cents / 100
    (s,) = recurring.apply_overrides(con, recurring.detect(df, today=date(2026, 5, 10)))
    assert s.status == "active"
    recurring.set_override(con, s.merchant, s.account, s.typical, "cancelled", date(2026, 5, 20))
    (s,) = recurring.apply_overrides(con, recurring.detect(df, today=date(2026, 5, 25)))
    assert (s.status, s.active, s.charged_after_cancel) == ("cancelled", False, False)

    _netflix(con, [6], amount=-1399)  # they charge anyway (and the price went up: still matches)
    reprocess(con)
    alerts.generate(con, today=date(2026, 6, 10))
    kinds = [r[0] for r in con.execute("SELECT kind FROM alerts")]
    assert "charged_after_cancel" in kinds and "missed_recurring" not in kinds

    recurring.set_override(con, s.merchant, s.account, s.typical, "ignored")
    df = metrics.transactions(con)
    df["amount"] = df.my_amount_eur_cents / 100
    assert recurring.apply_overrides(con, recurring.detect(df)) == []


def test_trip_candidates_prebooked_and_recurring():
    rows = [
        # (date, merchant, top, category, amount, tags)
        ("2026-06-15", "TAP", "Travel", "Travel > Flights", -400, ""),           # booked before
        ("2026-06-01", "Ryanair", "Travel", "Travel > Flights", -90, "Spain 2026"),  # another trip
        ("2026-03-01", "Old Hotel", "Travel", "Travel > Accommodation", -300, ""),  # outside lookback
        ("2026-09-05", "Kaza Katxupa", "Food", "Food > Restaurants", -30, ""),    # during
        ("2026-09-06", "Vodafone", "Housing", "Housing > Utilities > Internet & phone", -37, ""),
        ("2026-09-07", "Netflix", "Subscriptions", "Subscriptions > Streaming", -13, ""),
        ("2026-09-08", "Cash At Cabo Verde", "Cash", "Cash > ATM withdrawal", -140, ""),
    ]
    df = pd.DataFrame(rows, columns=["d", "merchant", "top", "category", "amount", "tags"])
    df["date"] = pd.to_datetime(df.d)
    df["id"] = range(len(df))
    df["account"], df["account_id"], df["flow"], df["one_off"] = "Main", 1, "expense", False
    out = trips.candidates(df, date(2026, 9, 4), date(2026, 9, 12), recurring_set={("Vodafone", "Main")},
                           tag="Cabo Verde", lookback_days=120)
    got = dict(zip(out.merchant, zip(out["when"], out.include)))
    assert got["TAP"] == ("booked before", True)
    assert got["Ryanair"] == ("booked before", False)
    assert "Old Hotel" not in got
    assert got["Kaza Katxupa"] == ("during", True) and got["Cash At Cabo Verde"] == ("during", True)
    assert not got["Vodafone"][1]
    assert not got["Netflix"][1]


def test_projection_math_deterministic_case():
    # zero volatility, 0% return, no fees/inflation: value = start + contributions
    plan = P.Plan(years=2, start_value=1000, monthly=100, contribution_growth=0.0, model="parametric", mu=0.0,
                  sigma=1e-9, ter=0.0, inflation=0.0, n_sims=50)
    r = P.simulate(plan)
    assert r.nominal[50][-1] == pytest.approx(1000 + 24 * 100, rel=1e-4)
    # +10% contributions and a lump sum show up exactly
    s = P.simulate(P.scenario(plan, contribution_change=0.1, lump_sums=((0, 500),)))
    assert s.nominal[50][-1] - r.nominal[50][-1] == pytest.approx(24 * 10 + 500, rel=1e-4)
    # growth compounds; stopping contributions caps the input
    g = P.contributions(P.scenario(plan, contribution_growth=0.1, stop_after_years=1))
    assert g[:12].sum() == pytest.approx(1200) and g[12:].sum() == 0


def test_projection_crn_and_fire_probability():
    plan = P.Plan(years=10, start_value=100_000, monthly=1000, model="parametric", mu=0.07, sigma=0.15,
                  fire_target=300_000, n_sims=2000)
    a, b = P.simulate(plan), P.simulate(P.scenario(plan, contribution_change=0.5))
    # same random paths: more money in is better on *every* percentile
    assert all(b.nominal[p][-1] > a.nominal[p][-1] for p in P.PCTS)
    assert np.all(np.diff(a.p_fire[::12]) >= -0.05)  # broadly increasing over time
    assert b.p_fire[-1] >= a.p_fire[-1]


def test_projection_historical_bootstrap_uses_blocks():
    hist = pd.Series(np.r_[np.full(24, 0.01), np.full(24, -0.01)],
                     index=pd.date_range("2020-01-31", periods=48, freq="ME"))
    r = P.simulate(P.Plan(years=1, start_value=1000, monthly=0, model="historical", ter=0, inflation=0,
                          n_sims=200), hist)
    # a 12-month block is all +1% or all -1% (or straddles once): end values cluster accordingly
    assert r.final_nominal.max() == pytest.approx(1000 * 1.01**12, rel=1e-6)
    assert r.final_nominal.min() == pytest.approx(1000 * 0.99**12, rel=1e-6)


def test_pace_budgets_and_report(con):
    _acc(con)
    insert_transactions(con, 1, "eb", [
        RawTxn(date(2026, 9, 3), -5000, "EUR", "COMPRA 3043 PINGO DOCE LISBOA"),
        RawTxn(date(2026, 10, 2), -8000, "EUR", "COMPRA 3043 PINGO DOCE LISBOA"),
        RawTxn(date(2026, 9, 24), 200000, "EUR", "TRANSFERENCIA - VENCIMENTO"),
    ])
    reprocess(con)
    con.execute("INSERT INTO budgets (category, monthly_eur) VALUES ('Food', 100)")
    df = metrics.transactions(con, metrics.Filters(start=date(2000, 1, 1), end=date(2026, 10, 10)))
    pace = metrics.month_pace(df, date(2026, 10, 10), months=1)
    assert pace.this_month.loc[10] == 80 and pace.typical.loc[10] == 50
    b = metrics.budget_status(con, df, date(2026, 10, 10))
    assert b.iloc[0].spent == 80 and b.iloc[0].used == pytest.approx(0.8)
    html = report.build(con, metrics.Filters(start=date(2026, 9, 1), end=date(2026, 10, 10)), cdn=True)
    assert html.startswith("<!doctype html>") and "Income vs expenses" in html and "cdn.plot.ly" in html
