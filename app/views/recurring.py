from datetime import date, timedelta

import streamlit as st
import ui

from fin.insights import recurring as rec


@st.cache_data(ttl=600, show_spinner=False)
def _series():
    return rec.to_frame(rec.detect(ui.all_txns_full()))


def render():
    st.title("Recurring payments")
    df = _series()
    if df.empty:
        st.info("No recurring payments detected yet (needs 3+ regular charges).")
        return
    active = df[df.active].sort_values("annual_cost", ascending=False)
    c1, c2, c3 = st.columns(3)
    c1.metric("Active recurring", len(active), border=True)
    c2.metric("Per month", ui.eur(active.monthly_cost.sum()), border=True)
    c3.metric("Per year", ui.eur(active.annual_cost.sum()), border=True)

    cfg = {
        "amount": st.column_config.NumberColumn("Last charge", format="€%.2f"),
        "typical": st.column_config.NumberColumn("Typical", format="€%.2f"),
        "monthly_cost": st.column_config.NumberColumn("Monthly", format="€%.2f"),
        "annual_cost": st.column_config.ProgressColumn("Annual", format="€%.0f", min_value=0,
                                                       max_value=float(df.annual_cost.max())),
        "price_change": st.column_config.NumberColumn("Price change", format="%+.2f €"),
        "last": st.column_config.DateColumn("Last"), "next_expected": st.column_config.DateColumn("Next"),
        "since": st.column_config.DateColumn("Since"),
    }
    cols = ["merchant", "category", "frequency", "amount", "typical", "monthly_cost", "annual_cost", "price_change",
            "last", "next_expected", "account", "charges", "since"]

    st.subheader("Upcoming (next 30 days)")
    soon = active[(active.next_expected >= date.today() - timedelta(days=3))
                  & (active.next_expected <= date.today() + timedelta(days=30))].sort_values("next_expected")
    if soon.empty:
        st.caption("Nothing expected in the next 30 days.")
    else:
        st.dataframe(soon[["next_expected", "merchant", "typical", "frequency", "account"]], hide_index=True,
                     width="stretch", column_config=cfg)
        st.caption(f"Expected total: {ui.eur(soon.typical.sum(), 2)}")

    st.subheader("Active")
    st.dataframe(active[cols], hide_index=True, width="stretch", column_config=cfg)
    stopped = df[~df.active]
    if not stopped.empty:
        with st.expander(f"Stopped / cancelled ({len(stopped)})"):
            st.dataframe(stopped[cols], hide_index=True, width="stretch", column_config=cfg)
    st.caption("Detection: same merchant and similar amount, 3+ charges at a regular interval "
               "(weekly → yearly). Amounts within 15% of each other count as one series.")
