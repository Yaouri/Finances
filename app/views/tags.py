from datetime import date, timedelta

import plotly.graph_objects as go
import streamlit as st
import ui

from fin import metrics
from fin.insights import trips

TCOLS = ["booking_date", "when", "account", "merchant", "category", "amount", "why_not"]


def _builder(c, df):
    existing = metrics.by_tag(df).tag.tolist()
    a, b, cc = st.columns([2, 2, 3])
    choice = a.selectbox("Trip", ["New trip…", *existing], key="trip_pick")
    name = a.text_input("Name", placeholder="e.g. Cabo Verde 2026", key="trip_name") if choice == "New trip…" else choice
    rng = b.date_input("Trip dates", value=(date.today() - timedelta(days=14), date.today()), key="trip_dates")
    acc = metrics.accounts(c)
    names = dict(zip(acc.id, acc.name))
    accs = cc.multiselect("Accounts", list(names), format_func=names.get, placeholder="All accounts", key="trip_acc")
    lookback = cc.slider("Look for bookings made up to … days before", 0, 365, trips.PREBOOK_LOOKBACK_DAYS, 15)
    if not name or not isinstance(rng, tuple) or len(rng) != 2:
        st.caption("Pick a name and a start and end date.")
        return
    cand = trips.candidates(df, rng[0], rng[1], accounts=tuple(accs), recurring_set=trips.recurring_pairs(c, df),
                            tag=name, lookback_days=lookback)
    already = cand.tags.str.split(", ").apply(lambda t: name in t)
    cand = cand[~already]
    if cand.empty:
        st.info("No new transactions to add for these dates.")
        return
    st.caption("Ticked rows will be tagged. Recurring payments (internet, gym, subscriptions) and home costs "
               "falling inside the dates are listed unticked; travel bookings paid before the trip are ticked.")
    view = cand[["id", "include", *TCOLS]]
    edited = st.data_editor(view, hide_index=True, width="stretch", key=f"trip_editor_{name}_{rng}",
                            disabled=TCOLS + ["id"], column_config={
                                "id": None, "include": st.column_config.CheckboxColumn("Include"),
                                "amount": st.column_config.NumberColumn(format="€%.2f"),
                                "booking_date": st.column_config.DateColumn("Date"),
                                "why_not": st.column_config.TextColumn("Note")})
    picked = edited[edited.include]
    st.caption(f"{len(picked)} selected · {ui.eur(-picked.amount.sum(), 2)}")
    if st.button(f"Tag {len(picked)} transactions as '{name}'", type="primary", disabled=picked.empty):
        n = trips.tag_ids(c, name.strip(), picked.id.tolist())
        ui.invalidate()
        st.toast(f"Tagged {n} transactions")
        st.rerun()


def render():
    st.title("Trips & tags")
    c = ui.con()
    df = ui.all_txns_full()
    tags = metrics.by_tag(df)

    with st.expander("Build or extend a trip", expanded=tags.empty, icon=":material/flight:"):
        _builder(c, df)

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
    span = sel[sel.top != "Travel"] if not sel.empty else sel
    days = (span.date.max() - span.date.min()).days + 1 if not span.empty else 0
    k = st.columns(4)
    k[0].metric("Total", ui.eur(-s.amount.sum()), border=True)
    k[1].metric("Booked before", ui.eur(-s[s.date < span.date.min()].amount.sum()) if days else "–", border=True,
                help="Travel purchases dated before the first on-trip spend")
    k[2].metric("Days", days or "–", border=True)
    k[3].metric("Per day (on trip)", ui.eur(-s[s.date >= span.date.min()].amount.sum() / days, 2) if days else "–",
                border=True)
    a, b = st.columns([2, 3], gap="large")
    with a:
        bycat = (-s.groupby("category").amount.sum()).sort_values(ascending=False)
        st.dataframe(bycat.round(2).rename("spent").reset_index(), hide_index=True, width="stretch",
                     column_config={"spent": st.column_config.NumberColumn("Spent", format="€%.2f")})
    with b:
        tv = sel.sort_values("date")[["id", "booking_date", "account", "merchant", "category", "amount", "notes"]]
        ev = st.dataframe(tv, hide_index=True, width="stretch", key="tag_tx", on_select="rerun",
                          selection_mode="multi-row", column_config={
                              "id": None, "amount": st.column_config.NumberColumn("Amount", format="€%.2f")})
        rows = tv.iloc[ev.selection.rows] if ev and ev.selection.rows else tv.iloc[0:0]
        if not rows.empty and st.button(f"Remove {len(rows)} from '{pick}'"):
            trips.untag_ids(c, pick, rows.id.tolist())
            ui.invalidate()
            st.rerun()
