"""Shared UI plumbing: connection, theme tokens, stable colours, chart styling, global filters.

Colour rules (validated palette, see dataviz skill): categorical slots in fixed order, colour
follows the entity (stored per category/account, never by rank), max 8 slots - the rest is
neutral "Other". Light and dark are separately stepped palettes.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from fin import db, metrics, viz
from fin.config import get_settings
from fin.viz import INK, SEQ_BLUE, SEQ_BLUE_DARK, SERIES  # noqa: F401 - re-exported for views

# --- theme tokens (fin.viz) -------------------------------------------------------------
OTHER = "Other"


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # noqa: BLE001
        return "light"


def ink(key: str) -> str:
    return INK[mode()][key]


def slot(i: int) -> str:
    return SERIES[mode()][(i - 1) % 8]


# --- data access ----------------------------------------------------------------------------
@st.cache_resource
def con() -> sqlite3.Connection:
    return db.connect(get_settings().db_path)


def invalidate() -> None:
    st.cache_data.clear()


@st.cache_data(ttl=600, show_spinner=False)
def txns(_f: metrics.Filters | None, key: tuple) -> pd.DataFrame:
    return metrics.transactions(con(), _f)


def tx_frame(f: metrics.Filters | None) -> pd.DataFrame:
    return txns(f, (f,) if f else ("all",))


@st.cache_data(ttl=600, show_spinner=False)
def all_txns_full() -> pd.DataFrame:
    """Unfiltered, joint at my share — for history-based insights (recurring, first-seen, ...)."""
    df = metrics.transactions(con())
    df["amount"] = df.my_amount_eur_cents / 100
    return df


# --- stable colours -------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def category_slots() -> dict[str, int]:
    """Top-level expense category -> slot 1..7, assigned once by all-time spend and persisted,
    so colours never move with filters. Everything else renders as neutral Other."""
    c = con()
    rows = dict(c.execute("SELECT name, color FROM categories WHERE parent_id IS NULL AND color LIKE 'slot:%'")
                .fetchall())
    if not rows:
        df = all_txns_full()
        spend = (-metrics.spending(df).groupby("top").amount.sum()).sort_values(ascending=False)
        spend = spend[~spend.index.isin(["Uncategorised", "Refunds & reimbursements"])]
        for i, name in enumerate(spend.index[:7], start=1):
            c.execute("UPDATE categories SET color = ? WHERE parent_id IS NULL AND name = ?", (f"slot:{i}", name))
        c.commit()
        rows = dict(c.execute("SELECT name, color FROM categories WHERE parent_id IS NULL AND color LIKE 'slot:%'")
                    .fetchall())
    return {k: int(v.split(":")[1]) for k, v in rows.items()}


def category_color(top: str) -> str:
    s = category_slots().get(top)
    return slot(s) if s else ink("other")


def fold_other(df: pd.DataFrame, name_col: str = "name") -> pd.DataFrame:
    """Categories without a slot fold into 'Other' (keeps <= 8 colours on screen)."""
    slots = category_slots()
    d = df.copy()
    d[name_col] = d[name_col].where(d[name_col].isin(slots.keys()), OTHER)
    return d


@st.cache_data(ttl=3600, show_spinner=False)
def account_slots() -> dict[int, int]:
    ids = [r[0] for r in con().execute("SELECT id FROM accounts ORDER BY id")]
    return {aid: i + 1 for i, aid in enumerate(ids)}


def account_color(account_id: int) -> str:
    return slot(account_slots().get(account_id, 8))


# --- formatting -----------------------------------------------------------------------------
def eur(x: float | None, decimals: int = 0, sign: bool = False) -> str:
    if x is None or pd.isna(x):
        return "–"
    s = f"{x:+,.{decimals}f}" if sign else f"{x:,.{decimals}f}"
    return f"€{s}" if not s.startswith(("-", "+")) else f"{s[0]}€{s[1:]}"


def pct(x: float | None, decimals: int = 0) -> str:
    return "–" if x is None or pd.isna(x) else f"{x:.{decimals}%}"


# --- charts ---------------------------------------------------------------------------------
def style(fig: go.Figure, height: int = 320, legend: bool = True, yfmt: str | None = None) -> go.Figure:
    return viz.style(fig, mode(), height, legend, yfmt)


def end_label(fig: go.Figure, x, y, text: str) -> None:
    """Direct label for a series' last point, lifted clear of the line."""
    fig.add_annotation(x=x, y=y, text=text, showarrow=False, xanchor="right", yanchor="bottom", yshift=8,
                       font=dict(color=ink("primary"), size=13))


def show(fig: go.Figure, data: pd.DataFrame | None = None, key: str | None = None, **kw):
    """Render a chart plus its table-view twin (every value reachable without hover/colour)."""
    ev = st.plotly_chart(fig, width="stretch", key=key, config={"displayModeBar": False}, **kw)
    if data is not None:
        with st.expander("Table view", expanded=False):
            st.dataframe(data, width="stretch", hide_index=True)
    return ev


# --- global filters -------------------------------------------------------------------------
PRESETS = {
    "This month": lambda t: (t.replace(day=1), t),
    "Last 3 months": lambda t: ((t.replace(day=1) - pd.DateOffset(months=2)).date(), t),
    "Year to date": lambda t: (t.replace(month=1, day=1), t),
    "Last 12 months": lambda t: ((t.replace(day=1) - pd.DateOffset(months=11)).date(), t),
    "All time": lambda t: (date(2000, 1, 1), t),
}


def sidebar_filters() -> metrics.Filters:
    c = con()
    today = date.today()
    with st.sidebar:
        st.markdown("**Filters**")
        preset = st.selectbox("Period", [*PRESETS, "Custom"], index=3, key="f_preset")
        if preset == "Custom":
            rng = st.date_input("Range", value=(today - timedelta(days=90), today), key="f_range")
            start, end = (rng if isinstance(rng, tuple) and len(rng) == 2 else (today - timedelta(days=90), today))
        else:
            start, end = PRESETS[preset](today)
        acc = metrics.accounts(c)
        names = dict(zip(acc.id, acc.name))
        chosen = st.multiselect("Accounts", list(names), format_func=names.get, key="f_accounts",
                                placeholder="All accounts")
        tops = [r[0] for r in c.execute(
            "SELECT name FROM categories WHERE parent_id IS NULL AND kind = 'expense' ORDER BY name")]
        cats = st.multiselect("Categories", tops, key="f_cats", placeholder="All categories")
        joint = st.segmented_control(
            "Joint account", ["my_share", "full", "exclude"], default="my_share", key="f_joint",
            format_func={"my_share": "My share", "full": "Full", "exclude": "Exclude"}.get,
        ) or "my_share"
        if start < date(2001, 1, 1):
            first = c.execute("SELECT min(booking_date) FROM transactions").fetchone()[0]
            start = date.fromisoformat(first) if first else start
        st.caption(f"{start:%d %b %Y} → {end:%d %b %Y}")
    return metrics.Filters(start=start, end=end, accounts=tuple(chosen), categories=tuple(cats), joint=joint)


def consent_banner() -> None:
    from fin import prefs
    from fin.ingest.enablebanking.consent import consent_status

    warn = prefs.get(con(), "consent_warn_days")
    for cs in consent_status(con()):
        if cs["days_left"] <= 0:
            st.error(f"**{cs['bank']}** bank access expired on {cs['valid_until']:%d %b}. Reconnect in Settings.",
                     icon=":material/link_off:")
        elif cs["days_left"] <= warn:
            st.warning(f"**{cs['bank']}** bank access expires in {cs['days_left']:.0f} days "
                       f"({cs['valid_until']:%d %b}). Reconnect in Settings.", icon=":material/schedule:")
