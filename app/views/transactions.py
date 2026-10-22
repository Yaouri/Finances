"""Searchable transactions with inline recategorisation, notes and tags, plus the review queue."""

import pandas as pd
import streamlit as st
import ui

from fin import metrics
from fin.categorize import manual
from fin.pipeline import reprocess

COLS = ["id", "booking_date", "account", "merchant", "description", "amount", "category", "notes", "tags",
        "category_source"]


def _editor(df: pd.DataFrame, key: str, suggestions: bool = False):
    c = ui.con()
    paths = [r[0] for r in c.execute("SELECT path FROM categories ORDER BY kind, path")]
    cols = COLS + (["suggestion", "ml_confidence"] if suggestions else [])
    view = (df if suggestions else df.sort_values(["date", "id"], ascending=False))[cols].copy()
    view["category"] = view.category.where(view.category != "Uncategorised", None)
    view["notes"] = view.notes.fillna("")
    edited = st.data_editor(
        view, key=key, hide_index=True, width="stretch", height=520,
        disabled=["id", "booking_date", "account", "merchant", "description", "amount", "category_source",
                  "suggestion", "ml_confidence"],
        column_config={
            "id": None,
            "booking_date": st.column_config.DateColumn("Date"),
            "amount": st.column_config.NumberColumn("Amount", format="€%.2f"),
            "category": st.column_config.SelectboxColumn("Category", options=paths, width="large"),
            "tags": st.column_config.TextColumn("Tags", help="comma separated; 'one-off' excludes from savings rate"),
            "category_source": st.column_config.TextColumn("Source", width="small",
                                                           help="manual / rule / ml / transfer / none"),
            "ml_confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=1,
                                                             format="percent", width="small"),
        },
    )
    changed = edited.merge(view, on="id", suffixes=("", "_old"))
    changed = changed[(changed.category.fillna("") != changed.category_old.fillna(""))
                      | (changed.notes != changed.notes_old) | (changed.tags != changed.tags_old)]
    if changed.empty:
        return
    st.caption(f"{len(changed)} unsaved change(s)")
    make_rules = st.checkbox("Also create a rule for each recategorised merchant", value=True, key=f"{key}_rules",
                             help="Future transactions from the same merchant get the same category")
    if st.button("Save changes", type="primary", key=f"{key}_save"):
        made = 0
        for r in changed.itertuples():
            recat = (r.category or "") != (r.category_old or "")
            try:
                rid = manual.set_category(c, int(r.id), r.category if recat else None,
                                          note=r.notes if r.notes != r.notes_old else None,
                                          make_rule=make_rules and recat and bool(r.category))
                made += bool(rid)
            except ValueError:
                pass  # no merchant to build a rule from: category still saved
            if r.tags != r.tags_old:
                manual.set_tags(c, int(r.id), [t for t in r.tags.split(",") if t.strip()])
        reprocess(c)
        ui.invalidate()
        st.toast(f"Saved {len(changed)} change(s)" + (f", {made} new rule(s)" if made else ""))
        st.rerun()


def render():
    f: metrics.Filters = st.session_state["filters"]
    st.title("Transactions")
    df = ui.tx_frame(f)
    review = df[(df.category == "Uncategorised") & ~df.flow.isin(["transfer", "investing"])]
    tab_all, tab_review = st.tabs(["All", f"Review queue ({len(review)})"])

    with tab_all:
        a, b, c2 = st.columns([3, 2, 2])
        q = a.text_input("Search", placeholder="merchant, description, category, note…", key="tx_q")
        flows = b.multiselect("Flow", ["expense", "income", "transfer", "investing"], key="tx_flow")
        src = c2.multiselect("Source", ["manual", "rule", "ml", "transfer", "none"], key="tx_src")
        v = df
        if q:
            hay = (v.merchant.fillna("") + " " + v.description + " " + v.category + " " + v.notes.fillna("")
                   + " " + v.tags)
            v = v[hay.str.contains(q, case=False, regex=False)]
        if flows:
            v = v[v.flow.isin(flows)]
        if src:
            v = v[v.category_source.fillna("none").isin(src)]
        st.caption(f"{len(v)} transactions · net {ui.eur(v.amount.sum(), 2, sign=True)}")
        _editor(v, "tx_all")

    with tab_review:
        if review.empty:
            st.success("Everything in this period is categorised.", icon=":material/check_circle:")
        else:
            st.caption("Uncategorised transactions in the selected period, biggest first. Pick a category; "
                       "a merchant rule is created so the next ones are automatic. The model's guess is shown "
                       "with its confidence (it auto-applies only at ≥ 70%, where it's ~99% right).")
            sugg = review[review.ml_confidence.fillna(0) >= 0.5]
            if not sugg.empty:
                with st.expander(f"Accept {len(sugg)} suggestion(s) with ≥ 50% confidence", icon=":material/auto_fix:"):
                    st.dataframe(sugg[["booking_date", "merchant", "amount", "suggestion", "ml_confidence"]],
                                 hide_index=True, width="stretch", column_config={
                                     "ml_confidence": st.column_config.ProgressColumn("Confidence", min_value=0,
                                                                                      max_value=1, format="percent"),
                                     "amount": st.column_config.NumberColumn(format="€%.2f")})
                    if st.button("Accept these", type="primary"):
                        c = ui.con()
                        for r in sugg.itertuples():
                            manual.set_category(c, int(r.id), r.suggestion)
                        reprocess(c)
                        ui.invalidate()
                        st.rerun()
            ordered = review.reindex(review.amount.abs().sort_values(ascending=False).index)
            _editor(ordered, "tx_review", suggestions=True)
