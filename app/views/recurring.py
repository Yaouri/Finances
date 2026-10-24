from datetime import date, timedelta

import streamlit as st
import ui

from fin.insights import recurring as rec


@st.cache_data(ttl=600, show_spinner=False)
def _series():
    return rec.to_frame(rec.apply_overrides(ui.con(), rec.detect(ui.all_txns_full())))


CFG = {
    "amount": st.column_config.NumberColumn("Last charge", format="€%.2f"),
    "typical": st.column_config.NumberColumn("Typical", format="€%.2f"),
    "monthly_cost": st.column_config.NumberColumn("Monthly", format="€%.2f"),
    "annual_cost": st.column_config.NumberColumn("Annual", format="€%.0f"),
    "price_change": st.column_config.NumberColumn("Price change", format="%+.2f €"),
    "last": st.column_config.DateColumn("Last"), "next_expected": st.column_config.DateColumn("Next"),
    "since": st.column_config.DateColumn("Since"), "cancelled_on": st.column_config.DateColumn("Cancelled"),
    "charged_after_cancel": st.column_config.CheckboxColumn("Charged after?"),
}
COLS = ["merchant", "category", "frequency", "amount", "typical", "monthly_cost", "annual_cost", "price_change",
        "last", "next_expected", "account", "charges", "since"]


def _selectable(df, key):
    ev = st.dataframe(df, hide_index=True, width="stretch", column_config=CFG, key=key,
                      on_select="rerun", selection_mode="multi-row")
    return df.iloc[ev.selection.rows] if ev and ev.selection.rows else df.iloc[0:0]


def render():
    st.title("Recurring payments")
    c = ui.con()
    df = _series()
    if df.empty:
        st.info("No recurring payments detected yet (needs 3+ regular charges).")
        return
    active = df[df.status == "active"].sort_values("annual_cost", ascending=False)
    cancelled = df[df.status == "cancelled"]
    stopped = df[df.status == "stopped"]

    k = st.columns(4)
    k[0].metric("Active", len(active), border=True)
    k[1].metric("Per month", ui.eur(active.monthly_cost.sum()), border=True)
    k[2].metric("Per year", ui.eur(active.annual_cost.sum()), border=True)
    saved = cancelled.annual_cost.sum()
    k[3].metric("Saved by cancelling", ui.eur(saved) + "/yr", border=True,
                help="Annual cost of the series you've marked as cancelled")

    bad = cancelled[cancelled.charged_after_cancel]
    for r in bad.itertuples():
        st.error(f"**{r.merchant}** charged {ui.eur(r.amount, 2)} on {r.last} although you cancelled it on "
                 f"{r.cancelled_on}.", icon=":material/report:")

    st.subheader("Upcoming (next 30 days)")
    soon = active[(active.next_expected >= date.today() - timedelta(days=3))
                  & (active.next_expected <= date.today() + timedelta(days=30))].sort_values("next_expected")
    if soon.empty:
        st.caption("Nothing expected in the next 30 days.")
    else:
        st.dataframe(soon[["next_expected", "merchant", "typical", "frequency", "account"]], hide_index=True,
                     width="stretch", column_config=CFG)
        st.caption(f"Expected total: {ui.eur(soon.typical.sum(), 2)}")

    st.subheader("Active")
    st.caption("Select rows, then mark them cancelled (stops 'missed charge' alerts and warns you if they "
               "charge again) or not recurring (a false positive).")
    sel = _selectable(active[COLS], "rec_active")
    if not sel.empty:
        a, b, d = st.columns([2, 2, 2], vertical_alignment="bottom")
        when = a.date_input("Cancelled on", value=date.today(), key="rec_when")
        if b.button(f"Mark {len(sel)} cancelled", type="primary", icon=":material/block:"):
            for r in sel.itertuples():
                rec.set_override(c, r.merchant, r.account, r.typical, "cancelled", when)
            ui.invalidate()
            st.rerun()
        if d.button("Not recurring", icon=":material/visibility_off:"):
            for r in sel.itertuples():
                rec.set_override(c, r.merchant, r.account, r.typical, "ignored")
            ui.invalidate()
            st.rerun()

    if not cancelled.empty:
        st.subheader("Cancelled by you")
        sel = _selectable(cancelled[["merchant", "typical", "annual_cost", "cancelled_on", "last",
                                     "charged_after_cancel", "account", "override_id"]], "rec_cancelled")
        if not sel.empty and st.button("Undo (mark active again)"):
            for r in sel.itertuples():
                rec.clear_override(c, int(r.override_id))
            ui.invalidate()
            st.rerun()

    if not stopped.empty:
        with st.expander(f"Stopped on their own ({len(stopped)}) · no charge for 1.6× their interval"):
            st.dataframe(stopped[COLS], hide_index=True, width="stretch", column_config=CFG)
    ignored = c.execute("SELECT id, merchant, account, typical FROM recurring_overrides WHERE status='ignored'").fetchall()
    if ignored:
        with st.expander(f"Hidden as not recurring ({len(ignored)})"):
            for r in ignored:
                x, y = st.columns([5, 1])
                x.write(f"{r['merchant']} · {ui.eur(r['typical'], 2)} · {r['account']}")
                if y.button("Restore", key=f"unhide{r['id']}"):
                    rec.clear_override(c, r["id"])
                    ui.invalidate()
                    st.rerun()
    st.caption("Detection: same merchant and similar amount, 3+ charges at a regular interval "
               "(weekly → yearly). Amounts within 15% of each other count as one series.")
