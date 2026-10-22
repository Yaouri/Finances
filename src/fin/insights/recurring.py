"""Recurring payments / subscriptions.

Per merchant, amounts are clustered (consecutive sorted amounts within 15% of each other) so
'Apple 0.99' and 'Apple 7.49' become separate series. A series is recurring when it has >= 3
charges whose gaps match a known period with low dispersion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

PERIODS = [  # name, nominal days, tolerance
    ("weekly", 7, 2), ("monthly", 30.4, 5), ("bimonthly", 61, 7), ("quarterly", 91, 10),
    ("half-yearly", 182, 15), ("yearly", 365, 20),
]
PER_YEAR = {"weekly": 52, "monthly": 12, "bimonthly": 6, "quarterly": 4, "half-yearly": 2, "yearly": 1}
EXCLUDED_TYPES = {"atm", "fx", "card_refund", "topup"}


@dataclass
class Series:
    key: str
    merchant: str
    category: str
    account: str
    period: str
    n: int
    first: date
    last: date
    next_expected: date
    amount: float          # latest charge (positive = cost)
    typical: float         # median charge
    previous: float | None
    active: bool

    @property
    def annual_cost(self) -> float:
        return self.typical * PER_YEAR[self.period]

    @property
    def monthly_cost(self) -> float:
        return self.annual_cost / 12

    @property
    def price_change(self) -> float | None:
        # ignore FX wobble on foreign-currency subscriptions: >= 3% and >= €0.25
        if self.previous is None or abs(self.amount - self.previous) < max(0.03 * self.previous, 0.25):
            return None
        return self.amount - self.previous


def _clusters(amounts: np.ndarray) -> list[np.ndarray]:
    order = np.argsort(amounts)
    groups, cur = [], [order[0]]
    for i in order[1:]:
        if amounts[i] <= amounts[cur[-1]] * 1.15 + 0.5:
            cur.append(i)
        else:
            groups.append(np.array(cur))
            cur = [i]
    groups.append(np.array(cur))
    return groups


def detect(df: pd.DataFrame, today: date | None = None) -> list[Series]:
    """df: metrics.transactions() frame (unfiltered by date is best)."""
    today = today or date.today()
    out: list[Series] = []
    exp = df[(df.flow == "expense") & (df.amount < 0) & ~df.descriptor_type.isin(EXCLUDED_TYPES)]
    for (merchant, account), g in exp.groupby(["merchant", "account"]):
        if len(g) < 3:
            continue
        amounts = -g.amount.to_numpy()
        for idx in _clusters(amounts):
            if len(idx) < 3:
                continue
            s = g.iloc[idx].sort_values("date")
            dates = s.date.dt.date.tolist()
            gaps = np.diff([d.toordinal() for d in dates])
            gaps = gaps[gaps > 0]
            if len(gaps) < 2:
                continue
            med = float(np.median(gaps))
            mad = float(np.median(np.abs(gaps - med)))
            period = next((name for name, days, tol in PERIODS if abs(med - days) <= tol and mad <= tol), None)
            if period is None:
                continue
            nominal = {p: d for p, d, _ in PERIODS}[period]
            nxt = dates[-1] + timedelta(days=round(nominal))
            vals = -s.amount.to_numpy()
            out.append(Series(
                key=f"{merchant}|{account}|{round(float(np.median(vals)))}",
                merchant=merchant, category=s.category.mode().iat[0], account=account, period=period,
                n=len(s), first=dates[0], last=dates[-1], next_expected=nxt,
                amount=float(vals[-1]), typical=float(np.median(vals)),
                previous=float(vals[-2]) if len(vals) > 1 else None,
                active=today <= dates[-1] + timedelta(days=round(nominal * 1.6)),
            ))
    return sorted(out, key=lambda r: -r.annual_cost)


def to_frame(series: list[Series]) -> pd.DataFrame:
    rows = [{
        "merchant": s.merchant, "category": s.category, "account": s.account, "frequency": s.period,
        "charges": s.n, "last": s.last, "next_expected": s.next_expected, "amount": s.amount, "typical": s.typical,
        "monthly_cost": s.monthly_cost, "annual_cost": s.annual_cost, "price_change": s.price_change,
        "active": s.active, "since": s.first,
    } for s in series]
    return pd.DataFrame(rows)
