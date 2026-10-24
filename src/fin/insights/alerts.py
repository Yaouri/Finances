"""Alert generation. Each alert has a stable fingerprint, so re-running never resurrects an alert
you marked seen/ok, and changing thresholds only adds new ones."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .. import metrics, prefs
from ..ingest.enablebanking.consent import consent_status
from . import recurring


@dataclass
class Alert:
    fingerprint: str
    kind: str
    severity: str  # info | warning | serious
    title: str
    detail: str = ""
    tx_id: int | None = None


def _eur(x: float) -> str:
    return f"€{abs(x):,.2f}"


def transaction_alerts(df: pd.DataFrame, p: dict, today: date) -> list[Alert]:
    out: list[Alert] = []
    lookback = pd.Timestamp(today - timedelta(days=int(p["alert_lookback_days"])))
    exp = df[(df.flow == "expense") & (df.amount < 0)].sort_values("date")
    history_start = exp.date.min()

    for r in exp[exp.date >= lookback].itertuples():
        amt = -r.amount
        if amt >= p["large_txn_eur"]:
            out.append(Alert(f"large:{r.id}", "large", "warning", f"Large payment: {_eur(amt)} at {r.merchant}",
                             f"{r.booking_date} · {r.account} · {r.category}", r.id))

    # first-time merchant: no earlier charge, and history long enough to know (>= 90 days before)
    first_seen = exp.groupby("merchant").date.transform("min")
    firsts = exp[(exp.date == first_seen) & (exp.date >= lookback) & (-exp.amount >= p["first_merchant_eur"])
                 & (exp.date - history_start > pd.Timedelta(days=90))]
    for r in firsts.drop_duplicates("merchant").itertuples():
        out.append(Alert(f"newmerchant:{r.id}", "new_merchant", "info",
                         f"First purchase at {r.merchant}: {_eur(r.amount)}", f"{r.booking_date} · {r.category}", r.id))

    # unusual for merchant: robust z vs that merchant's own history
    for merchant, g in exp.groupby("merchant"):
        if len(g) < 6:
            continue
        vals = -g.amount.to_numpy()
        med = np.median(vals)
        mad = np.median(np.abs(vals - med)) * 1.4826 or max(med * 0.1, 1.0)
        for r in g[g.date >= lookback].itertuples():
            z = (-r.amount - med) / mad
            if z >= p["merchant_outlier_z"] and -r.amount > 2 * med and -r.amount >= 20:
                out.append(Alert(f"outlier:{r.id}", "unusual_amount", "warning",
                                 f"Unusual amount at {merchant}: {_eur(r.amount)} (typical {_eur(med)})",
                                 f"{r.booking_date} · {r.account}", r.id))

    # possible duplicates: same account + merchant + amount within 2 days, >= €10
    recent = exp[(exp.date >= lookback) & (-exp.amount >= 10)]
    for _, g in recent.groupby(["account_id", "merchant", "amount"]):
        if len(g) < 2:
            continue
        g = g.sort_values("date")
        for a, b in zip(g.itertuples(), list(g.itertuples())[1:]):
            if (b.date - a.date).days <= 2:
                out.append(Alert(f"dup:{a.id}:{b.id}", "duplicate", "warning",
                                 f"Possible duplicate: {_eur(b.amount)} at {b.merchant} twice",
                                 f"{a.booking_date} and {b.booking_date} · {b.account}", b.id))

    # bank fees: one alert per month with the total
    fees = df[(df.category.str.startswith("Fees & taxes")) & (df.date >= lookback) & (df.amount < 0)]
    for month, g in fees.groupby("month"):
        out.append(Alert(f"fees:{month:%Y-%m}:{len(g)}", "fees", "info",
                         f"Bank fees & charges in {month:%b %Y}: {_eur(g.amount.sum())}",
                         ", ".join(sorted(g.category.str.split(" > ").str[-1].unique())) + f" · {len(g)} charges"))
    return out


def drift_alerts(df: pd.DataFrame, p: dict, today: date) -> list[Alert]:
    out = []
    mat = metrics.category_month_matrix(df)
    if mat.shape[1] < 4:
        return out
    this_month = pd.Timestamp(today.replace(day=1))
    months = [m for m in mat.columns if m <= this_month]
    for m in months[-3:]:  # recent months only
        prev = [x for x in mat.columns if x < m][-6:]
        if len(prev) < 3:
            continue
        for cat, row in mat.iterrows():
            base, cur = row[prev].mean(), row[m]
            if base >= 25 and cur >= base * (1 + p["drift_pct"]) and cur - base >= p["drift_min_eur"]:
                partial = " so far" if m == this_month else ""
                out.append(Alert(f"drift:{cat}:{m:%Y-%m}", "drift", "info",
                                 f"{cat} {cur / base - 1:+.0%} vs your 6-month average in {m:%b %Y}{partial}",
                                 f"{_eur(cur)} vs {_eur(base)} average"))
    return out


def recurring_alerts(series: list[recurring.Series], today: date) -> list[Alert]:
    out = []
    for s in series:
        if s.charged_after_cancel:
            out.append(Alert(f"aftercancel:{s.key}:{s.last}", "charged_after_cancel", "serious",
                             f"{s.merchant} charged {_eur(s.amount)} after you cancelled it",
                             f"cancelled {s.cancelled_on} · charged {s.last} · {s.account}"))
        if not s.active:
            continue
        if s.price_change and today - s.last <= timedelta(days=60):
            direction = "up" if s.price_change > 0 else "down"
            out.append(Alert(f"price:{s.key}:{s.last}", "price_change", "warning" if s.price_change > 0 else "info",
                             f"{s.merchant} price {direction}: {_eur(s.previous)} → {_eur(s.amount)}",
                             f"{s.period} · {s.account}"))
        late = (today - s.next_expected).days
        if s.period in ("weekly", "monthly") and 7 < late <= 45:
            out.append(Alert(f"missed:{s.key}:{s.next_expected}", "missed_recurring", "info",
                             f"Expected {s.merchant} charge hasn't arrived",
                             f"due {s.next_expected} ({late} days ago) · usually {_eur(s.typical)}"))
    return out


def consent_alerts(con: sqlite3.Connection, p: dict) -> list[Alert]:
    out = []
    for c in consent_status(con):
        if c["days_left"] <= p["consent_warn_days"]:
            sev = "serious" if c["days_left"] <= 3 else "warning"
            out.append(Alert(f"consent:{c['session_id']}", "consent", sev,
                             f"{c['bank']} access {'expired' if c['days_left'] <= 0 else 'expires'} "
                             f"{c['valid_until']:%d %b %Y}", "Reconnect it in Settings → Banks"))
    return out


def generate(con: sqlite3.Connection, today: date | None = None) -> int:
    """Upsert all alerts; returns how many are new."""
    today = today or date.today()
    p = prefs.all_prefs(con)
    df = metrics.transactions(con)
    df["amount"] = df.my_amount_eur_cents / 100
    series = recurring.apply_overrides(con, recurring.detect(df, today))
    alerts = (transaction_alerts(df, p, today) + drift_alerts(df, p, today)
              + recurring_alerts(series, today) + consent_alerts(con, p))
    before = con.execute("SELECT count(*) FROM alerts").fetchone()[0]
    con.executemany(
        """INSERT INTO alerts (fingerprint, kind, severity, tx_id, title, detail) VALUES (?,?,?,?,?,?)
           ON CONFLICT(fingerprint) DO UPDATE SET title = excluded.title, detail = excluded.detail""",
        [(a.fingerprint, a.kind, a.severity, a.tx_id, a.title, a.detail) for a in alerts],
    )
    con.commit()
    return con.execute("SELECT count(*) FROM alerts").fetchone()[0] - before


def set_status(con: sqlite3.Connection, alert_ids: list[int], status: str) -> None:
    if status not in ("new", "seen", "ok"):
        raise ValueError(status)
    con.executemany("UPDATE alerts SET status = ? WHERE id = ?", [(status, i) for i in alert_ids])
    con.commit()
