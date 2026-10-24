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


def _merchants(df: pd.DataFrame):
    """Bulk: one category per merchant -> a merchant rule (applies to past and future transactions;
    single transactions you set by hand still win)."""
    c = ui.con()
    paths = [r[0] for r in c.execute("SELECT path FROM categories WHERE kind IN ('expense','income') ORDER BY path")]
    exp = df[df.flow.isin(["expense", "income"]) & df.merchant_id.notna()]
    g = (exp.groupby("merchant")
         .agg(n=("id", "count"), total=("amount", "sum"), last=("date", "max"),
              uncategorised=("category", lambda x: (x == "Uncategorised").sum()),
              category=("category", lambda x: x[x != "Uncategorised"].mode().iat[0]
                        if (x != "Uncategorised").any() else None))
         .reset_index())
    only_open = st.toggle("Only merchants with uncategorised transactions", value=True, key="m_open")
    if only_open:
        g = g[g.uncategorised > 0]
    q = st.text_input("Filter merchants", key="m_q")
    if q:
        g = g[g.merchant.str.contains(q, case=False, regex=False)]
    g = g.sort_values(["uncategorised", "total"], ascending=[False, True])
    st.caption("Set a category for a merchant and save: a rule is created, so every past and future "
               "transaction from that merchant follows it (single transactions you set by hand still win).")
    edited = st.data_editor(g, hide_index=True, width="stretch", height=480, key="m_editor",
                            disabled=["merchant", "n", "total", "last", "uncategorised"],
                            column_config={
                                "category": st.column_config.SelectboxColumn("Category", options=paths, width="large"),
                                "total": st.column_config.NumberColumn("Total", format="€%.2f"),
                                "last": st.column_config.DateColumn("Last"),
                                "n": st.column_config.NumberColumn("#")})
    changed = edited.merge(g[["merchant", "category"]], on="merchant", suffixes=("", "_old"))
    changed = changed[changed.category.notna() & (changed.category != changed.category_old)]
    if not changed.empty and st.button(f"Save {len(changed)} merchant rule(s)", type="primary"):
        import re as _re

        from fin.categorize import rules as R
        from fin.categorize import taxonomy

        ids = taxonomy.path_ids(c)
        for r in changed.itertuples():
            pat = f"^{_re.escape(r.merchant)}$"
            c.execute("DELETE FROM rules WHERE field = 'merchant' AND pattern = ? AND coalesce(name,'') <> 'seed'", (pat,))
            R.add_rule(c, "merchant", pat, ids[r.category], name="merchant (bulk)")
        c.commit()
        st.toast(f"{len(changed)} rule(s) saved · {reprocess(c).uncategorised} still uncategorised")
        ui.invalidate()
        st.rerun()


def render():
    f: metrics.Filters = st.session_state["filters"]
    st.title("Transactions")
    df = ui.tx_frame(f)
    review = df[(df.category == "Uncategorised") & ~df.flow.isin(["transfer", "investing"])]
    tab_all, tab_review, tab_merch = st.tabs(["All", f"Review queue ({len(review)})", "By merchant"])
    with tab_merch:
        _merchants(ui.all_txns_full())

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
