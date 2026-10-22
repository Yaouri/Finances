"""Spending breakdown with drill-down: category -> sub-category -> merchant -> transactions."""

import plotly.graph_objects as go
import streamlit as st
import ui

from fin import metrics

LEVELS = ["top", "sub", "merchant"]


def _crumbs():
    path = st.session_state.setdefault("drill", [])
    cols = st.columns([1] + [1] * len(path) + [6 - len(path)])
    if cols[0].button("All categories", type="tertiary"):
        st.session_state["drill"] = []
        st.rerun()
    for i, p in enumerate(path):
        if cols[i + 1].button(f"› {p.split(' > ')[-1]}", type="tertiary", key=f"crumb{i}"):
            st.session_state["drill"] = path[: i + 1]
            st.rerun()
    return path


def render():
    f: metrics.Filters = st.session_state["filters"]
    st.title("Spending")
    df = ui.tx_frame(f)
    s = metrics.spending(df)
    if s.empty:
        st.info("No spending in this range.")
        return

    path = _crumbs()
    level = LEVELS[min(len(path), 2)]
    parent = path[-1] if path else None
    data = metrics.by_category(df, level, parent)
    if len(path) == 3:  # merchant chosen -> its transactions only
        data = data.iloc[0:0]
    top_name = path[0] if path else None

    total = data.spent.sum() if not data.empty else 0
    left, right = st.columns([3, 2], gap="large")
    with left:
        if data.empty:
            st.caption("Pick a bar to drill in; transactions for the selection are listed below.")
        else:
            d = data.head(20).iloc[::-1]
            colors = ([ui.category_color(n) for n in d.name] if level == "top"
                      else [ui.category_color(top_name)] * len(d))
            labels = [n.split(" > ")[-1] for n in d.name]
            fig = go.Figure(go.Bar(
                x=d.spent, y=labels, orientation="h", marker_color=colors, customdata=d.name,
                text=[f"{ui.eur(v)}  ·  {v / total:.0%}" for v in d.spent], textposition="outside", cliponaxis=False,
                hovertemplate="%{y}: %{x:,.2f} € (%{customdata})<extra></extra>",
            ))
            fig = ui.style(fig, max(260, 34 * len(d) + 40), legend=False)
            fig.update_layout(hovermode="closest", margin=dict(r=110))
            fig.update_xaxes(visible=False)
            st.caption(f"{ui.eur(total)} total · click a bar to drill down")
            ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                                 key=f"drill_{len(path)}_{parent}", config={"displayModeBar": False})
            pts = ev.selection.points if ev and ev.selection else []
            if pts:
                st.session_state["drill"] = [*path, pts[0]["customdata"]]
                st.rerun()
            with st.expander("Table view"):
                st.dataframe(data, hide_index=True, width="stretch")

    with right:
        st.subheader("Top merchants" if not path else "Trend")
        if not path:
            tm = metrics.top_merchants(df, 12)
            st.dataframe(tm, hide_index=True, width="stretch",
                         column_config={"spent": st.column_config.NumberColumn("Spent", format="€%.2f"),
                                        "n": st.column_config.NumberColumn("#"),
                                        "last": st.column_config.DateColumn("Last")})
        else:
            sel = s[s.top == path[0]]
            if len(path) >= 2:
                sel = sel[sel.category == path[1]]
            if len(path) == 3:
                sel = sel[sel.merchant == path[2]]
            m = (-sel.groupby("month").amount.sum())
            fig = go.Figure(go.Bar(x=m.index, y=m.values, marker_color=ui.category_color(path[0]),
                                   hovertemplate="%{x|%b %Y}: %{y:,.0f} €<extra></extra>"))
            fig = ui.style(fig, 260, legend=False)
            fig.update_layout(hovermode="closest")
            ui.show(fig)

    # --- transactions of the current selection --------------------------------------------
    sel = s
    if path:
        sel = sel[sel.top == path[0]]
    if len(path) >= 2:
        sel = sel[sel.category == path[1]]
    if len(path) == 3:
        sel = sel[sel.merchant == path[2]]
    st.subheader(f"Transactions · {path[-1].split(' > ')[-1] if path else 'all spending'}")
    st.dataframe(
        sel.sort_values("date", ascending=False)[["booking_date", "account", "merchant", "description", "category",
                                                   "amount", "tags", "notes"]],
        hide_index=True, width="stretch", height=320,
        column_config={"amount": st.column_config.NumberColumn("Amount", format="€%.2f"),
                       "booking_date": st.column_config.DateColumn("Date")},
    )

    # --- trends per category: heatmap (sequential, one hue) -------------------------------
    if not path:
        st.subheader("Monthly spending by category")
        mat = metrics.category_month_matrix(df)
        mat = mat.loc[mat.sum(axis=1).sort_values(ascending=False).index]
        scale = ui.SEQ_BLUE if ui.mode() == "light" else ui.SEQ_BLUE_DARK
        fig = go.Figure(go.Heatmap(
            z=mat.values, x=mat.columns, y=mat.index, colorscale=[[i / (len(scale) - 1), c] for i, c in enumerate(scale)],
            xgap=2, ygap=2, hovertemplate="%{y} · %{x|%b %Y}: %{z:,.0f} €<extra></extra>",
            colorbar=dict(thickness=10, outlinewidth=0, tickfont=dict(color=ui.ink("muted"))),
        ))
        fig = ui.style(fig, 30 * len(mat) + 80, legend=False)
        fig.update_layout(hovermode="closest")
        fig.update_xaxes(dtick="M1", tickformat="%b\n%y")
        fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
        ui.show(fig, mat.round(0).rename(columns=lambda m: f"{m:%Y-%m}").reset_index())
