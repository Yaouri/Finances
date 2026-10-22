from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import ui

from fin import metrics


def render():
    f: metrics.Filters = st.session_state["filters"]
    st.title("Cash flow")
    df = ui.tx_frame(f)
    cf = metrics.monthly_cashflow(df)
    if cf.empty:
        st.info("No transactions in this range.")
        return
    tot = metrics.period_totals(df)
    # savings rate only for complete months that had income (the running month has no salary yet)
    this_month = pd.Timestamp(date.today().replace(day=1))
    full = cf[(cf.income > 0) & (cf.index < this_month)]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Income", ui.eur(tot["income"]), border=True)
    c2.metric("Expenses", ui.eur(tot["expenses"]), border=True)
    c3.metric("Saved", ui.eur(tot["net"], sign=True), border=True)
    c4.metric("Savings rate", ui.pct(tot["savings_rate"]), border=True,
              help="Over the whole period: (income − expenses) ÷ income")
    c5.metric("Invested", ui.eur(tot["invested"]), border=True, help="Net deposits to brokers")
    if cf.one_off.abs().sum():
        st.caption(f"Excluded one-offs in this period: {ui.eur(cf.one_off.sum(), sign=True)} "
                   "(tagged `one-off`; still in net worth).")

    st.subheader("Income vs expenses")
    fig = go.Figure()
    fig.add_bar(x=cf.index, y=cf.income, name="Income", marker_color=ui.slot(1), hovertemplate="%{y:,.0f} €")
    fig.add_bar(x=cf.index, y=cf.expenses, name="Expenses", marker_color=ui.slot(2), hovertemplate="%{y:,.0f} €")
    fig = ui.style(fig, 320)
    fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
    ui.show(fig, cf.round(2).reset_index(names="month"), key="cf_bars")

    a, b = st.columns(2, gap="large")
    with a:
        st.subheader("Net savings")
        colors = [ui.ink("pos") if v >= 0 else ui.ink("neg") for v in cf.net]
        fig = go.Figure(go.Bar(x=cf.index, y=cf.net, marker_color=colors, hovertemplate="%{y:,.0f} €<extra></extra>"))
        fig = ui.style(fig, 280, legend=False)
        fig.update_layout(hovermode="closest")
        fig.update_xaxes(dtick="M1", tickformat="%b\n%y")
        ui.show(fig)
    with b:
        st.subheader("Savings rate")
        roll = (full.net.rolling(6, min_periods=3).sum() / full.income.rolling(6, min_periods=3).sum())
        fig = go.Figure()
        fig.add_scatter(x=full.index, y=full.savings_rate, name="Monthly", mode="lines+markers",
                        line=dict(width=2, color=ui.slot(1)), marker=dict(size=8, line=dict(width=2, color=ui.ink("surface"))),
                        hovertemplate="%{y:.0%}")
        fig.add_scatter(x=roll.index, y=roll, name="6-month", mode="lines", line=dict(width=2, color=ui.slot(7)),
                        hovertemplate="%{y:.0%}")
        avg = tot["savings_rate"]
        if avg is not None:
            fig.add_hline(y=avg, line=dict(color=ui.ink("muted"), width=1),
                          annotation_text=f"period {avg:.0%}", annotation_font_color=ui.ink("muted"))
        fig = ui.style(fig, 280, yfmt=".0%")
        lo = full.savings_rate.min() if not full.empty else 0
        fig.update_yaxes(range=[max(min(lo, 0) - 0.05, -1.0), 1.0])
        st.caption("Complete months only. Axis clipped at −100%.") if lo < -1 else None
        fig.update_xaxes(dtick="M1", tickformat="%b\n%y")
        ui.show(fig)
