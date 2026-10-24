from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import ui

from fin import metrics
from fin.insights import alerts as alerts_mod
from fin.invest import portfolio


@st.cache_data(ttl=600, show_spinner=False)
def _networth(f: metrics.Filters) -> pd.DataFrame:
    return metrics.networth_series(ui.con(), f)


@st.cache_data(ttl=600, show_spinner=False)
def _balances() -> pd.DataFrame:
    return metrics.current_balances(ui.con())


def render():
    f: metrics.Filters = st.session_state["filters"]
    today = date.today()
    st.title("Overview")

    # --- headline tiles ------------------------------------------------------------------
    nw_all = _networth(metrics.Filters(start=today - timedelta(days=400), end=today, accounts=f.accounts, joint=f.joint))
    month = metrics.Filters(start=today.replace(day=1), end=today, accounts=f.accounts, joint=f.joint)
    mt = metrics.period_totals(ui.tx_frame(month))
    inv = portfolio.summary(ui.con())

    c1, c2, c3, c4, c5 = st.columns(5)
    if not nw_all.empty:
        now = nw_all.networth.iloc[-1]
        ago = nw_all.networth.asof(pd.Timestamp(today - timedelta(days=30)))
        c1.metric("Net worth", ui.eur(now), ui.eur(now - ago, sign=True) + " · 30d", border=True)
        c2.metric("Cash", ui.eur(nw_all.cash.iloc[-1]), border=True)
    c3.metric("Investments", ui.eur(inv["value_eur"]),
              f"{ui.eur(inv['unrealised_eur'], sign=True)} ({inv['unrealised_pct']:+.1%})", border=True)
    c4.metric(f"{today:%B}: income − expenses", ui.eur(mt["net"], sign=True),
              f"{ui.eur(mt['income'])} in · {ui.eur(mt['expenses'])} out", delta_color="off", border=True)
    c5.metric("Savings rate (month)", ui.pct(mt["savings_rate"]), border=True,
              help="(income − expenses) ÷ income. Excludes transfers, investing and one-offs.")

    left, right = st.columns([3, 2], gap="large")

    # --- net worth over time -------------------------------------------------------------
    with left:
        st.subheader("Net worth")
        nw = _networth(f)
        if nw.empty:
            st.info("No balance history in this range.")
        else:
            w = nw.resample("W").last() if len(nw) > 400 else nw
            fig = go.Figure()
            for col, name, i in (("investments", "Investments", 1), ("cash", "Cash", 3)):
                fig.add_scatter(x=w.index, y=w[col], name=name, stackgroup="nw", mode="lines",
                                line=dict(width=2, color=ui.slot(i)), hovertemplate="%{y:,.0f} €")
            ui.end_label(fig, w.index[-1], w.networth.iloc[-1], ui.eur(w.networth.iloc[-1]))
            ui.show(ui.style(fig, 340), w[["cash", "investments", "networth"]].round(0).reset_index(names="date"))

    # --- balances per account ------------------------------------------------------------
    with right:
        st.subheader("Balances")
        b = _balances()
        b = b[b.balance.notna()].copy()
        b["shown"] = b.my_balance if f.joint == "my_share" else b.balance
        if f.joint == "exclude":
            b = b[b.is_joint == 0]
        b = b.sort_values("shown")
        fig = go.Figure(go.Bar(
            x=b.shown, y=b.name, orientation="h", marker_color=[ui.account_color(i) for i in b.id],
            text=[ui.eur(v) for v in b.shown], textposition="outside", cliponaxis=False,
            hovertemplate="%{y}: %{x:,.2f} €<extra></extra>",
        ))
        fig = ui.style(fig, 340, legend=False)
        fig.update_layout(hovermode="closest", margin=dict(r=70))
        fig.update_xaxes(visible=False)
        ui.show(fig, b[["name", "institution", "balance", "my_balance", "as_of", "last_synced_at"]])

    # --- this month: pace + budgets -----------------------------------------------------
    hist_f = metrics.Filters(start=date(2000, 1, 1), end=today, accounts=f.accounts, joint=f.joint)
    full = ui.tx_frame(hist_f)
    p_col, b_col = st.columns([3, 2], gap="large")
    with p_col:
        st.subheader(f"Spending pace · {today:%B}")
        pace = metrics.month_pace(full, today)
        fig = go.Figure()
        fig.add_scatter(x=pace.index, y=pace.typical, name="Typical month (last 6)", mode="lines",
                        line=dict(width=2, color=ui.ink("muted")), hovertemplate="%{y:,.0f} €")
        fig.add_scatter(x=pace.index, y=pace.this_month, name="This month", mode="lines",
                        line=dict(width=2, color=ui.slot(2)), hovertemplate="%{y:,.0f} €")
        now, typ = pace.this_month.dropna().iloc[-1], pace.typical.get(today.day, 0)
        ui.end_label(fig, today.day, now, ui.eur(now))
        fig = ui.style(fig, 260)
        fig.update_xaxes(title="day of month", dtick=5)
        ui.show(fig, pace.round(0).reset_index(names="day"))
        if typ:
            st.caption(f"{ui.eur(now)} spent so far vs {ui.eur(typ)} by day {today.day} in a typical month "
                       f"({now / typ - 1:+.0%}).")
    with b_col:
        st.subheader("Budgets")
        bs = metrics.budget_status(ui.con(), full, today)
        if bs.empty:
            st.caption("No budgets yet: set them in Settings → Budgets (suggested from your averages).")
        for r in bs.itertuples():
            over = r.used > 1
            ahead = r.used > r.pace + 0.1
            note = "over budget" if over else ("ahead of pace" if ahead else "on track")
            icon = ":material/error:" if over else (":material/warning:" if ahead else ":material/check:")
            st.progress(min(r.used, 1.0), text=f"{icon} **{r.category}** · {ui.eur(r.spent)} of {ui.eur(r.budget)} "
                                               f"· {note}")
        if not bs.empty:
            st.caption(f"{today.day / pd.Timestamp(today).days_in_month:.0%} of the month gone.")

    # --- alerts --------------------------------------------------------------------------
    st.subheader("Latest alerts")
    render_alerts(limit=8)


def render_alerts(limit: int | None = None, statuses=("new",)):
    c = ui.con()
    q = f"SELECT * FROM alerts WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY created_at DESC, id DESC"
    rows = c.execute(q + (f" LIMIT {int(limit)}" if limit else ""), statuses).fetchall()
    if not rows:
        st.success("Nothing new. All alerts reviewed.", icon=":material/check_circle:")
        return
    icons = {"serious": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}
    for r in rows:
        a, b, cbtn = st.columns([8, 1, 1], vertical_alignment="center")
        a.markdown(f"{icons.get(r['severity'], '')} **{r['title']}**  \n:gray[{r['detail'] or ''}]")
        if b.button("Seen", key=f"seen{r['id']}", width="stretch"):
            alerts_mod.set_status(c, [r["id"]], "seen")
            st.rerun()
        if cbtn.button("OK", key=f"ok{r['id']}", type="primary", width="stretch",
                       help="Mark as expected / fine"):
            alerts_mod.set_status(c, [r["id"]], "ok")
            st.rerun()
    left = c.execute("SELECT count(*) FROM alerts WHERE status='new'").fetchone()[0]
    if limit and left > limit:
        st.caption(f"{left - limit} more on Settings → Alerts.")
