"""Monte Carlo projections of the investment portfolio (monthly steps).

Return models:
- "historical": block bootstrap of monthly total returns of a global-equity proxy (12-month
  blocks keep momentum / crash clustering). Only ~13 years of data, mostly a bull market, so it
  leans optimistic; compare with the parametric model.
- "parametric": lognormal with a chosen expected annual return and volatility.

Baseline and scenario share the same random draws (common random numbers), so the difference
between them is the effect of your changes, not simulation noise.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PROXY = "VWRL.AS"   # FTSE All-World, EUR listing, since 2012; adjusted closes = dividends reinvested
PT_CGT = 0.28        # Portuguese tax on realised capital gains (autonomous rate)
PCTS = (10, 25, 50, 75, 90)


@dataclass(frozen=True)
class Plan:
    years: int = 30
    start_value: float = 0.0
    start_cost: float = 0.0              # cost basis of start_value (for the tax estimate)
    monthly: float = 1000.0              # contribution per month (today)
    contribution_growth: float = 0.02    # yearly increase of the contribution (raises)
    contribution_change: float = 0.0     # scenario knob: +0.10 = invest 10% more every month
    lump_sums: tuple[tuple[int, float], ...] = field(default=())  # (month, amount) one-off buys
    stop_after_years: int | None = None  # stop contributing after N years (e.g. FIRE date)
    ter: float = 0.0022                  # fund running costs per year
    inflation: float = 0.02              # for "today's euros"
    model: str = "historical"            # historical | parametric
    mu: float = 0.07                     # parametric: expected annual return (nominal, arithmetic)
    sigma: float = 0.15                  # parametric: annual volatility
    fire_target: float | None = None     # in today's euros
    n_sims: int = 5000
    seed: int = 7


@dataclass
class Result:
    months: pd.DatetimeIndex
    nominal: dict[int, np.ndarray]       # percentile -> path
    real: dict[int, np.ndarray]
    contributed: np.ndarray              # cumulative contributions (deterministic), nominal
    p_fire: np.ndarray | None            # probability of being at/above the FIRE target, per month
    final_nominal: np.ndarray            # all simulated end values
    final_real: np.ndarray
    after_tax_final_real: np.ndarray


# --- returns ------------------------------------------------------------------------------------

def proxy_monthly_returns(con: sqlite3.Connection, fetch=None) -> pd.Series:
    """Monthly total returns of the proxy, cached in `prices` (ticker PROXY, adjusted close)."""
    (last,) = con.execute("SELECT max(date) FROM prices WHERE ticker = ?", (PROXY,)).fetchone()
    if last is None or (date.today() - date.fromisoformat(last)).days > 7:
        try:
            if fetch is None:
                import yfinance as yf

                h = yf.Ticker(PROXY).history(start="2012-01-01", auto_adjust=True)["Close"].dropna()
                h.index = h.index.tz_localize(None).date if h.index.tz is not None else h.index.date
            else:
                h = fetch(PROXY)
            con.executemany(
                "INSERT INTO prices (ticker, date, close, currency) VALUES (?,?,?, 'EUR') "
                "ON CONFLICT(ticker, date) DO UPDATE SET close = excluded.close",
                [(PROXY, d.isoformat(), float(v)) for d, v in h.items()],
            )
            con.commit()
        except Exception as e:  # noqa: BLE001 - offline: use what's cached
            log.warning("proxy price fetch failed, using cache: %s", e)
    df = pd.read_sql_query("SELECT date, close FROM prices WHERE ticker = ? ORDER BY date", con, params=(PROXY,))
    if df.empty:
        return pd.Series(dtype=float)
    s = pd.Series(df.close.values, index=pd.to_datetime(df.date)).resample("ME").last()
    return s.pct_change().dropna()


def _draw_returns(plan: Plan, n_months: int, hist: pd.Series | None) -> np.ndarray:
    rng = np.random.default_rng(plan.seed)
    if plan.model == "historical" and hist is not None and len(hist) >= 36:
        r = hist.to_numpy()
        block = 12
        n_blocks = -(-n_months // block)
        starts = rng.integers(0, len(r) - block + 1, size=(plan.n_sims, n_blocks))
        idx = (starts[..., None] + np.arange(block)).reshape(plan.n_sims, -1)[:, :n_months]
        return r[idx]
    # lognormal: E[1+R_annual] = 1 + mu
    s_m = plan.sigma / np.sqrt(12)
    m_m = np.log1p(plan.mu) / 12 - 0.5 * s_m**2
    return np.expm1(rng.normal(m_m, s_m, size=(plan.n_sims, n_months)))


def hist_stats(hist: pd.Series) -> dict:
    if hist.empty:
        return {}
    ann = (1 + hist).prod() ** (12 / len(hist)) - 1
    return {"cagr": float(ann), "vol": float(hist.std() * np.sqrt(12)), "start": hist.index.min().date(),
            "end": hist.index.max().date(), "worst_12m": float((1 + hist).rolling(12).apply(np.prod).min() - 1)}


# --- simulation ---------------------------------------------------------------------------------

def contributions(plan: Plan) -> np.ndarray:
    n = plan.years * 12
    m = np.arange(n)
    c = plan.monthly * (1 + plan.contribution_change) * (1 + plan.contribution_growth) ** (m // 12)
    if plan.stop_after_years is not None:
        c[m >= plan.stop_after_years * 12] = 0
    for month, amount in plan.lump_sums:
        if 0 <= month < n:
            c[month] += amount
    return c


def simulate(plan: Plan, hist: pd.Series | None = None, start: date | None = None) -> Result:
    n = plan.years * 12
    rets = _draw_returns(plan, n, hist) - plan.ter / 12
    contrib = contributions(plan)
    v = np.empty((plan.n_sims, n))
    cur = np.full(plan.n_sims, plan.start_value, dtype=float)
    for t in range(n):
        cur = (cur + contrib[t]) * (1 + rets[:, t])  # contribute at the start of the month
        v[:, t] = cur
    defl = (1 + plan.inflation) ** (np.arange(1, n + 1) / 12)
    real = v / defl
    start = start or date.today().replace(day=1)
    months = pd.date_range(pd.Timestamp(start) + pd.offsets.MonthEnd(0), periods=n, freq="ME")
    cost = plan.start_cost + contrib.sum()
    after_tax = v[:, -1] - PT_CGT * np.clip(v[:, -1] - cost, 0, None)
    return Result(
        months=months,
        nominal={p: np.percentile(v, p, axis=0) for p in PCTS},
        real={p: np.percentile(real, p, axis=0) for p in PCTS},
        contributed=plan.start_value + np.cumsum(contrib),
        p_fire=(real >= plan.fire_target).mean(axis=0) if plan.fire_target else None,
        final_nominal=v[:, -1], final_real=real[:, -1], after_tax_final_real=after_tax / defl[-1],
    )


def first_month_with_prob(res: Result, prob: float = 0.5) -> pd.Timestamp | None:
    if res.p_fire is None:
        return None
    hit = np.nonzero(res.p_fire >= prob)[0]
    return res.months[hit[0]] if hit.size else None


def milestones(res: Result, years: tuple[int, ...] = (5, 10, 15, 20, 25, 30, 40), real: bool = True) -> pd.DataFrame:
    paths = res.real if real else res.nominal
    rows = []
    for y in years:
        i = y * 12 - 1
        if i < len(res.months):
            rows.append({"year": y, "date": res.months[i].date(), "contributed": res.contributed[i],
                         **{f"p{p}": paths[p][i] for p in PCTS},
                         "p_fire": float(res.p_fire[i]) if res.p_fire is not None else None})
    return pd.DataFrame(rows)


def scenario(base: Plan, **changes) -> Plan:
    return replace(base, **changes)
