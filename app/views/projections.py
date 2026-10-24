"""Monte Carlo projections + FIRE: baseline vs a what-if scenario on the same random paths."""

from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import ui

from fin import metrics
from fin import projections as P
from fin.invest import portfolio


@st.cache_data(ttl=3600, show_spinner=False)
def _hist() -> pd.Series:
    return P.proxy_monthly_returns(ui.con())


@st.cache_data(ttl=600, show_spinner=False)
def _defaults() -> dict:
    """Starting points from your own data: last 12 complete months, joint at your share."""
    today = date.today()
    start = (pd.Timestamp(today.replace(day=1)) - pd.DateOffset(months=12)).date()
    end = (pd.Timestamp(today.replace(day=1)) - pd.Timedelta(days=1)).date()
    cf = metrics.monthly_cashflow(metrics.transactions(ui.con(), metrics.Filters(start=start, end=end)))
    s = portfolio.summary(ui.con())
    return {"value": s["value_eur"], "cost": s["invested_eur"],
            "monthly_saving": float(max(cf.net.median(), 0)) if not cf.empty else 1000.0,
            "annual_spend": float(cf.expenses.sum()) if not cf.empty else 24000.0}


@st.cache_data(ttl=600, show_spinner="Simulating…")
def _run(plan: P.Plan, hist: pd.Series) -> P.Result:
    return P.simulate(plan, hist)


def _fan(fig, res: P.Result, real: bool, color_i: int, name: str, band: bool):
    paths = res.real if real else res.nominal
    x = res.months
    color = ui.slot(color_i)
    if band:
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        for lo, hi, alpha, label in ((10, 90, 0.12, "10–90%"), (25, 75, 0.22, "25–75%")):
            fig.add_scatter(x=x, y=paths[hi], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip")
            fig.add_scatter(x=x, y=paths[lo], mode="lines", line=dict(width=0), fill="tonexty",
                            fillcolor=f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{alpha})", name=f"{name} {label}",
                            hoverinfo="skip")
    fig.add_scatter(x=x, y=paths[50], mode="lines", name=f"{name} median", line=dict(width=2, color=color),
                    hovertemplate="%{y:,.0f} €")


def render():
    st.title("Projections")
    d = _defaults()
    hist = _hist()
    hs = P.hist_stats(hist)

    with st.sidebar:
        st.markdown("**Projection inputs**")
        years = st.slider("Horizon (years)", 5, 50, 30)
        start_value = st.number_input("Starting portfolio (€)", value=round(d["value"], -2), step=1000.0)
        monthly = st.number_input("Monthly investment (€)", value=round(d["monthly_saving"], -1), step=50.0,
                                  help="Default: your median monthly net savings over the last 12 months")
        growth = st.slider("Contribution growth per year", 0.0, 0.06, 0.02, 0.005, format="percent",
                           help="Raises: how much the monthly amount grows each year")
        st.markdown("**Returns**")
        model = st.segmented_control("Model", ["parametric", "historical"], default="parametric",
                                     format_func={"parametric": "Assumptions", "historical": "Historical"}.get) or "parametric"
        mu, sigma = 0.07, 0.16
        if model == "parametric":
            mu = st.slider("Expected return (nominal, yearly)", 0.0, 0.12, 0.07, 0.005, format="percent")
            sigma = st.slider("Volatility (yearly)", 0.05, 0.30, 0.16, 0.01, format="percent")
        elif hs:
            st.caption(f"Bootstrap of {P.PROXY} {hs['start']:%Y}–{hs['end']:%Y}: {hs['cagr']:.1%}/yr, "
                       f"vol {hs['vol']:.0%}, worst 12 months {hs['worst_12m']:.0%}. Mostly a bull market, "
                       "so optimistic.")
        ter = st.number_input("Fund costs (TER %)", value=0.22, step=0.01) / 100
        inflation = st.slider("Inflation", 0.0, 0.05, 0.02, 0.0025, format="percent")
        st.markdown("**FIRE**")
        spend = st.number_input("Yearly spending in retirement (€, today)", value=round(d["annual_spend"], -2),
                                step=500.0, help="Default: your last 12 months of expenses (your share)")
        swr = st.slider("Safe withdrawal rate", 0.025, 0.05, 0.04, 0.0025, format="percent")
        real = st.toggle("Show in today's euros", value=True)

    base = P.Plan(years=years, start_value=start_value, start_cost=d["cost"], monthly=monthly,
                  contribution_growth=growth, model=model, mu=mu, sigma=sigma, ter=ter, inflation=inflation,
                  fire_target=spend / swr)

    st.markdown("##### What if…")
    a, b, c_, e = st.columns(4)
    more = a.slider("Invest more / less each month", -50, 200, 10, 5, format="%d%%") / 100
    lump = b.number_input("One-off extra buy (€)", value=0.0, step=1000.0, help="e.g. buying more shares now")
    lump_year = c_.number_input("…in year", min_value=0, max_value=years - 1, value=0)
    stop = e.number_input("Stop investing after (years, 0 = never)", min_value=0, max_value=years, value=0)
    scen = P.scenario(base, contribution_change=more,
                      lump_sums=((int(lump_year) * 12, lump),) if lump else (),
                      stop_after_years=int(stop) or None)

    rb, rs = _run(base, hist), _run(scen, hist)
    paths_b, paths_s = (rb.real, rs.real) if real else (rb.nominal, rs.nominal)
    unit = "today's €" if real else "nominal €"

    k = st.columns(5)
    k[0].metric(f"Median in {years}y", ui.eur(paths_s[50][-1]),
                ui.eur(paths_s[50][-1] - paths_b[50][-1], sign=True) + " vs baseline", border=True)
    k[1].metric("Pessimistic (10th pct)", ui.eur(paths_s[10][-1]), border=True)
    k[2].metric("Optimistic (90th pct)", ui.eur(paths_s[90][-1]), border=True)
    fi_b, fi_s = P.first_month_with_prob(rb), P.first_month_with_prob(rs)
    k[3].metric("FIRE number", ui.eur(base.fire_target), f"{spend:,.0f} €/yr at {swr:.1%}", delta_color="off",
                border=True)
    k[4].metric("50% chance of FI by", f"{fi_s:%b %Y}" if fi_s is not None else "not within horizon",
                (f"{(fi_b - fi_s).days / 365.25:+.1f} yr sooner" if fi_b is not None and fi_s is not None
                 and fi_b != fi_s else None), border=True)

    st.subheader(f"Portfolio value ({unit})")
    fig = go.Figure()
    _fan(fig, rs, real, 1, "Scenario", band=True)
    _fan(fig, rb, real, 2, "Baseline", band=False)
    fig.add_scatter(x=rs.months, y=rs.contributed / ((1 + inflation) ** (np.arange(1, len(rs.months) + 1) / 12)
                                                     if real else 1),
                    mode="lines", name="Money put in", line=dict(width=1.5, color=ui.ink("muted")),
                    hovertemplate="%{y:,.0f} €")
    if real:  # the target is in today's euros
        fig.add_hline(y=base.fire_target, line=dict(color=ui.ink("axis"), width=1),
                      annotation_text="FIRE number", annotation_font_color=ui.ink("muted"))
    ui.end_label(fig, rs.months[-1], paths_s[50][-1], ui.eur(paths_s[50][-1]))
    ui.show(ui.style(fig, 400), P.milestones(rs, real=real).round(0))

    l_, r_ = st.columns(2, gap="large")
    with l_:
        st.subheader("Chance of reaching your FIRE number")
        fig = go.Figure()
        fig.add_scatter(x=rs.months, y=rs.p_fire, name="Scenario", mode="lines", line=dict(width=2, color=ui.slot(1)),
                        hovertemplate="%{y:.0%}")
        fig.add_scatter(x=rb.months, y=rb.p_fire, name="Baseline", mode="lines", line=dict(width=2, color=ui.slot(2)),
                        hovertemplate="%{y:.0%}")
        fig = ui.style(fig, 300, yfmt=".0%")
        fig.update_yaxes(range=[0, 1.02])
        ui.show(fig)
    with r_:
        st.subheader(f"Outcomes in {years} years")
        q = pd.DataFrame({
            "": ["Pessimistic (p10)", "Median (p50)", "Optimistic (p90)"],
            "Baseline": [paths_b[p][-1] for p in (10, 50, 90)],
            "Scenario": [paths_s[p][-1] for p in (10, 50, 90)],
        })
        q["Difference"] = q.Scenario - q.Baseline
        st.dataframe(q, hide_index=True, width="stretch", column_config={
            c: st.column_config.NumberColumn(format="€%.0f") for c in ("Baseline", "Scenario", "Difference")})
        at = np.percentile(rs.after_tax_final_real if real else rs.after_tax_final_real * (1 + inflation) ** years, 50)
        st.caption(f"Scenario median after 28% tax on gains if all sold at the end: {ui.eur(at)}. "
                   f"Money put in over {years} years: {ui.eur(rs.contributed[-1] - start_value)} "
                   f"(baseline {ui.eur(rb.contributed[-1] - start_value)}).")
        st.caption("Each run draws 5,000 market paths; baseline and scenario use the same ones, so the "
                   "difference is your change, not luck. Not advice — assumptions drive everything.")
