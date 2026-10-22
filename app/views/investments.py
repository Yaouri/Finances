import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import ui

from fin import metrics
from fin.invest import portfolio


@st.cache_data(ttl=600, show_spinner=False)
def _series(end) -> pd.DataFrame:
    return portfolio.daily_series(ui.con(), end=end)


@st.cache_data(ttl=600, show_spinner=False)
def _income() -> pd.DataFrame:
    c = ui.con()
    df = pd.read_sql_query("SELECT * FROM invest_income", c)
    if df.empty:
        return df
    fx = dict(c.execute("SELECT date, close FROM prices WHERE ticker = 'EURUSD=X'").fetchall())
    last = list(fx.values())[-1] if fx else 1.1
    df["eur"] = [a if ccy == "EUR" else a / fx.get(d, last) for a, ccy, d in zip(df.amount, df.currency, df.date)]
    return df


def render():
    f: metrics.Filters = st.session_state["filters"]
    st.title("Investments")
    c = ui.con()
    s = portfolio.summary(c)
    inc = _income()
    divs = inc[inc.kind.isin(["dividend", "dividend_tax"])].eur.sum() if not inc.empty else 0.0
    cash = c.execute("""SELECT amount_cents FROM balances b JOIN accounts a ON a.id = b.account_id
                        WHERE a.key = 'manual:degiro-cash' ORDER BY as_of DESC LIMIT 1""").fetchone()

    k = st.columns(5)
    k[0].metric("Market value", ui.eur(s["value_eur"]), border=True)
    k[1].metric("Invested (cost incl. fees)", ui.eur(s["invested_eur"]), border=True)
    k[2].metric("Unrealised", ui.eur(s["unrealised_eur"], sign=True), f"{s['unrealised_pct']:+.1%}", border=True)
    k[3].metric("Realised", ui.eur(s["realised_eur"], sign=True), border=True)
    k[4].metric("Dividends (net)", ui.eur(divs, 2), help="NVIDIA/Alphabet dividends after US withholding",
                border=True)
    if cash:
        st.caption(f"Broker cash: {ui.eur(cash[0] / 100, 2)} · fees paid to date {ui.eur(s['fees_eur'], 2)}")
    approx = c.execute("SELECT count(*) FROM lots WHERE superseded = 0 AND note LIKE 'Approximate%'").fetchone()[0]
    if approx:
        st.info("Part of the cost basis is approximate (Millennium-era VWCE at ~€98, date unknown). "
                "Edit the lot below if you find the real purchases.", icon=":material/info:")

    st.subheader("Value vs contributions")
    d = _series(f.end)
    d = d[(d.index.date >= f.start)]
    if not d.empty:
        w = d.resample("W").last() if len(d) > 400 else d
        fig = go.Figure()
        fig.add_scatter(x=w.index, y=w.value_eur, name="Market value", mode="lines",
                        line=dict(width=2, color=ui.slot(1)), hovertemplate="%{y:,.0f} €")
        fig.add_scatter(x=w.index, y=w.contributed_eur, name="Net contributions", mode="lines",
                        line=dict(width=2, color=ui.slot(2), shape="hv"), hovertemplate="%{y:,.0f} €")
        ui.end_label(fig, w.index[-1], w.value_eur.iloc[-1], ui.eur(w.value_eur.iloc[-1]))
        ui.show(ui.style(fig, 340), w[["value_eur", "contributed_eur"]].round(0).reset_index(names="date"))

    a, b = st.columns([3, 2], gap="large")
    with a:
        st.subheader("Positions")
        rows = [{"name": p.name, "ticker": p.ticker, "units": p.quantity, "price": p.price, "value": p.value_eur,
                 "cost": p.cost_eur, "gain": p.unrealised_eur, "gain_pct": (p.unrealised_eur / p.cost_eur) if p.cost_eur else None,
                 "weight": (p.value_eur or 0) / s["value_eur"] if s["value_eur"] else None, "price_date": p.price_date}
                for p in sorted(s["positions"], key=lambda p: -(p.value_eur or 0))]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "price": st.column_config.NumberColumn("Price", format="€%.2f"),
            "value": st.column_config.NumberColumn("Value", format="€%.0f"),
            "cost": st.column_config.NumberColumn("Cost", format="€%.0f"),
            "gain": st.column_config.NumberColumn("Gain", format="%+.0f €"),
            "gain_pct": st.column_config.NumberColumn("Gain %", format="percent"),
            "weight": st.column_config.ProgressColumn("Weight", format="percent", min_value=0, max_value=1),
        })
    with b:
        st.subheader("Income")
        if inc.empty:
            st.caption("No dividends or interest imported.")
        else:
            g = inc.groupby("kind").eur.sum().rename({"dividend": "Dividends", "dividend_tax": "Withholding tax",
                                                      "interest": "Interest"})
            st.dataframe(g.round(2).reset_index(), hide_index=True, width="stretch",
                         column_config={"eur": st.column_config.NumberColumn("EUR", format="€%.2f")})

    with st.expander("Lots"):
        lots = pd.read_sql_query("""SELECT id, trade_date, broker, ticker, quantity, price, fees, total_eur, kind,
                                           superseded, note FROM lots ORDER BY trade_date DESC""", c)
        edited = st.data_editor(lots, hide_index=True, width="stretch", key="lots_editor",
                                disabled=["id", "broker", "ticker", "kind", "total_eur"])
        if st.button("Save lot changes"):
            for r in edited.itertuples():
                total = -(r.quantity * r.price + r.fees) if r.quantity > 0 else (-r.quantity * r.price - r.fees)
                c.execute("""UPDATE lots SET trade_date=?, quantity=?, price=?, fees=?, superseded=?, note=?,
                             total_eur=CASE WHEN source='manual' THEN ? ELSE total_eur END, locked=1 WHERE id=?""",
                          (r.trade_date, r.quantity, r.price, r.fees, int(r.superseded), r.note, round(total, 2), r.id))
            c.commit()
            ui.invalidate()
            st.rerun()
        st.caption("Manual lots recompute their cost from quantity × price + fees. Edited lots are locked "
                   "against later DEGIRO re-imports.")
