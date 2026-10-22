from datetime import date, timedelta

import plotly.graph_objects as go
import streamlit as st
import ui

from fin import metrics
from fin.categorize import manual
from fin.pipeline import reprocess


def render():
    st.title("Trips & tags")
    c = ui.con()
    df = ui.all_txns_full()
    tags = metrics.by_tag(df)

    with st.expander("Tag a trip (date range)", expanded=tags.empty, icon=":material/flight:"):
        a, b, cc = st.columns([2, 2, 3])
        name = a.text_input("Tag", placeholder="e.g. Cabo Verde 2026")
        rng = b.date_input("Dates", value=(date.today() - timedelta(days=14), date.today()))
        acc = metrics.accounts(c)
        names = dict(zip(acc.id, acc.name))
        accs = cc.multiselect("Accounts", list(names), format_func=names.get, placeholder="All accounts")
        if st.button("Tag spending in range", type="primary", disabled=not name or len(rng) != 2):
            n = manual.tag_range(c, name.strip(), rng[0].isoformat(), rng[1].isoformat(), accs)
            reprocess(c)
            ui.invalidate()
            st.toast(f"Tagged {n} transactions with '{name}'")
            st.rerun()
        st.caption("Tags only expenses. Fine-tune single transactions on the Transactions page (Tags column).")

    if tags.empty:
        st.info("No tags yet.")
        return
    st.subheader("Spending per tag")
    t = tags.sort_values("spent")
    fig = go.Figure(go.Bar(x=t.spent, y=t.tag, orientation="h", marker_color=ui.slot(1),
                           text=[ui.eur(v) for v in t.spent], textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}: %{x:,.2f} €<extra></extra>"))
    fig = ui.style(fig, max(200, 36 * len(t) + 40), legend=False)
    fig.update_layout(hovermode="closest", margin=dict(r=90))
    fig.update_xaxes(visible=False)
    ui.show(fig, tags)

    pick = st.selectbox("Tag details", tags.tag.tolist())
    sel = df[df.tags.str.split(", ").apply(lambda x: pick in x)]
    s = metrics.spending(sel) if pick != "one-off" else sel
    a, b = st.columns([2, 3], gap="large")
    with a:
        bycat = (-s.groupby("category").amount.sum()).sort_values(ascending=False)
        st.dataframe(bycat.round(2).rename("spent").reset_index(), hide_index=True, width="stretch",
                     column_config={"spent": st.column_config.NumberColumn("Spent", format="€%.2f")})
        days = (sel.date.max() - sel.date.min()).days + 1 if not sel.empty else 0
        if days:
            st.caption(f"{days} days · {ui.eur(-s.amount.sum() / days, 2)} per day")
    with b:
        st.dataframe(sel.sort_values("date")[["booking_date", "account", "merchant", "category", "amount", "notes"]],
                     hide_index=True, width="stretch",
                     column_config={"amount": st.column_config.NumberColumn("Amount", format="€%.2f")})
