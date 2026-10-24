"""Pure query/aggregation layer for the dashboard: (connection, Filters) -> tidy DataFrames.

The UI never writes SQL; changing a chart means changing a function here (or just its view).
Amounts are EUR, negative = money out. `amount` already reflects the joint-account mode.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from .. import balances
from ..categorize.taxonomy import SEP
from ..invest import portfolio

JOINT_MODES = ("my_share", "full", "exclude")


@dataclass(frozen=True)
class Filters:
    start: date
    end: date
    accounts: tuple[int, ...] = ()        # empty = all
    categories: tuple[str, ...] = ()      # top-level names; empty = all
    joint: str = "my_share"               # my_share | full | exclude
    tags: tuple[str, ...] = field(default=())

    @staticmethod
    def default(today: date | None = None) -> Filters:
        today = today or date.today()
        return Filters(start=date(today.year - 1, today.month, 1), end=today)


def transactions(con: sqlite3.Connection, f: Filters | None = None) -> pd.DataFrame:
    """All live transactions with enrichment. Filtering by date/account/category/joint applied."""
    df = pd.read_sql_query(
        """SELECT t.id, t.booking_date, t.value_date, t.account_id, a.name AS account, a.is_joint,
                  t.amount_cents, t.currency, t.description, t.counterparty_name,
                  e.descriptor_type, e.merchant_id, m.name AS merchant, e.category_id, c.path AS category,
                  c.kind AS category_kind, e.category_source, e.confidence, e.transfer_group, e.flow,
                  e.amount_eur_cents, e.my_amount_eur_cents, u.notes, u.reviewed,
                  e.ml_category_id, c2.path AS suggestion, e.ml_confidence,
                  (SELECT group_concat(g.name, ', ') FROM tx_tags x JOIN tags g ON g.id = x.tag_id
                    WHERE x.tx_id = t.id) AS tags
           FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           LEFT JOIN tx_enriched e ON e.tx_id = t.id
           LEFT JOIN merchants m ON m.id = e.merchant_id
           LEFT JOIN categories c ON c.id = e.category_id
           LEFT JOIN categories c2 ON c2.id = e.ml_category_id
           LEFT JOIN tx_user u ON u.tx_id = t.id
           WHERE t.duplicate_of IS NULL""",
        con,
    )
    df["date"] = pd.to_datetime(df.booking_date)
    df["month"] = df.date.dt.to_period("M").dt.to_timestamp()
    df["category"] = df.category.fillna("Uncategorised")
    df["top"] = df.category.str.split(SEP).str[0]
    df["sub"] = df.category.str.split(SEP).str[1].fillna(df.top)
    df["merchant"] = df.merchant.fillna(df.description.str.slice(0, 32))
    df["tags"] = df.tags.fillna("")
    df["one_off"] = df.tags.str.contains(r"\bone-off\b")
    df["flow"] = df.flow.fillna("expense")
    if f is None:
        df["amount"] = df.amount_eur_cents / 100
        return df

    if f.joint == "exclude":
        df = df[df.is_joint == 0]
    col = "amount_eur_cents" if f.joint == "full" else "my_amount_eur_cents"
    df = df.assign(amount=df[col] / 100)
    m = (df.date.dt.date >= f.start) & (df.date.dt.date <= f.end)
    if f.accounts:
        m &= df.account_id.isin(f.accounts)
    if f.categories:
        m &= df.top.isin(f.categories)
    if f.tags:
        m &= df.tags.apply(lambda s: any(t in s.split(", ") for t in f.tags))
    return df[m].copy()


def spending(df: pd.DataFrame) -> pd.DataFrame:
    """Expense rows only (refunds inside expense categories net against spending); one-offs out."""
    return df[(df.flow == "expense") & ~df.one_off]


def monthly_cashflow(df: pd.DataFrame) -> pd.DataFrame:
    """Per month: income, expenses (positive number), net savings, savings rate, investing."""
    core = df[~df.one_off]
    g = core.groupby(["month", "flow"]).amount.sum().unstack(fill_value=0)
    out = pd.DataFrame(index=g.index)
    out["income"] = g.get("income", 0.0)
    out["expenses"] = -g.get("expense", 0.0)
    out["net"] = out.income - out.expenses
    out["savings_rate"] = (out.net / out.income).where(out.income > 0)
    out["invested"] = -g.get("investing", 0.0)
    oneoff = df[df.one_off].groupby("month").amount.sum()
    out["one_off"] = oneoff.reindex(out.index).fillna(0.0)
    return out.sort_index()


def period_totals(df: pd.DataFrame) -> dict:
    cf = monthly_cashflow(df)
    inc, exp = cf.income.sum(), cf.expenses.sum()
    return {"income": inc, "expenses": exp, "net": inc - exp, "savings_rate": (inc - exp) / inc if inc else None,
            "invested": cf.invested.sum()}


def by_category(df: pd.DataFrame, level: str = "top", parent: str | None = None) -> pd.DataFrame:
    """Spending by top-level category, by sub-category within `parent`, or by merchant."""
    s = spending(df)
    if parent is not None:
        s = s[(s.top == parent) if level == "sub" else (s.category == parent)]
    key = {"top": "top", "sub": "category", "merchant": "merchant"}[level]
    g = s.groupby(key).agg(spent=("amount", lambda x: -x.sum()), n=("id", "count")).reset_index()
    return g[g.spent > 0].sort_values("spent", ascending=False).rename(columns={key: "name"})


def category_month_matrix(df: pd.DataFrame) -> pd.DataFrame:
    s = spending(df)
    return (-s.pivot_table(index="top", columns="month", values="amount", aggfunc="sum", fill_value=0)).clip(lower=0)


def top_merchants(df: pd.DataFrame, n: int = 15) -> pd.DataFrame:
    s = spending(df)
    g = s.groupby("merchant").agg(spent=("amount", lambda x: -x.sum()), n=("id", "count"),
                                  category=("category", lambda x: x.mode().iat[0]),
                                  last=("date", "max")).reset_index()
    return g.sort_values("spent", ascending=False).head(n)


def by_tag(df: pd.DataFrame) -> pd.DataFrame:
    s = df[(df.flow == "expense") & (df.tags != "")].copy()
    s["tag"] = s.tags.str.split(", ")
    s = s.explode("tag")
    s = s[s.tag != "one-off"]
    return (s.groupby("tag").agg(spent=("amount", lambda x: -x.sum()), n=("id", "count"),
                                 first=("date", "min"), last=("date", "max"))
            .reset_index().sort_values("last", ascending=False))


# --- balances & net worth -------------------------------------------------------------------

def accounts(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM accounts WHERE active = 1 ORDER BY id", con)


def networth_series(con: sqlite3.Connection, f: Filters) -> pd.DataFrame:
    """Daily cash (joint accounts at my share unless mode=full; excluded if mode=exclude) and
    investments. Account filter applies to cash only."""
    acc = accounts(con).set_index("id")
    bal = balances.all_accounts(con, f.end)
    cols = []
    for aid in bal.columns:
        a = acc.loc[aid]
        if f.accounts and aid not in f.accounts:
            continue
        if a.is_joint and f.joint == "exclude":
            continue
        share = a.my_share if (a.is_joint and f.joint == "my_share") else 1.0
        cols.append(bal[aid] * share)
    cash = pd.concat(cols, axis=1).sum(axis=1, min_count=1) if cols else pd.Series(dtype=float)
    inv = portfolio.daily_series(con, end=f.end)
    out = pd.DataFrame({"cash": cash})
    out = out.join(inv[["value_eur", "contributed_eur"]].rename(
        columns={"value_eur": "investments", "contributed_eur": "contributed"}), how="outer")
    if out.empty or not isinstance(out.index, pd.DatetimeIndex):
        return pd.DataFrame(columns=["cash", "investments", "contributed", "networth"],
                            index=pd.DatetimeIndex([]))
    out[["cash", "investments"]] = out[["cash", "investments"]].ffill().fillna(0)
    out["networth"] = out.cash + out.investments
    return out[(out.index.date >= f.start) & (out.index.date <= f.end)]


def current_balances(con: sqlite3.Connection) -> pd.DataFrame:
    acc = accounts(con)
    bal = balances.all_accounts(con)
    last = bal.ffill().iloc[-1] if not bal.empty else pd.Series(dtype=float)
    snap = pd.read_sql_query("SELECT account_id, max(as_of) as_of FROM balances GROUP BY account_id", con)
    acc = acc.merge(snap, left_on="id", right_on="account_id", how="left")
    acc["balance"] = acc.id.map(last)
    acc["my_balance"] = acc.balance * acc.my_share.where(acc.is_joint == 1, 1.0)
    return acc[["id", "name", "institution", "kind", "is_joint", "my_share", "balance", "my_balance",
                "as_of", "last_synced_at", "source"]]


def month_bounds(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    return today.replace(day=1), today


def trailing(today: date, days: int) -> date:
    return today - timedelta(days=days)


# --- pace & budgets -----------------------------------------------------------------------------

def month_pace(df: pd.DataFrame, today: date, months: int = 6) -> pd.DataFrame:
    """Cumulative spending by day of month: this month vs the average of the previous `months`
    complete months. `df` should be unfiltered by date (joint mode already applied)."""
    s = spending(df).copy()
    s["spent"] = -s.amount
    s["day"] = s.date.dt.day
    this = pd.Timestamp(today.replace(day=1))
    prev = [this - pd.DateOffset(months=i) for i in range(1, months + 1)]
    days = pd.RangeIndex(1, 32)
    cur = s[s.month == this].groupby("day").spent.sum().reindex(days, fill_value=0).cumsum()
    cur = cur[cur.index <= today.day]
    hist = (s[s.month.isin(prev)].groupby(["month", "day"]).spent.sum().unstack(fill_value=0)
            .reindex(columns=days, fill_value=0).cumsum(axis=1))
    typical = hist.mean(axis=0) if not hist.empty else pd.Series(0.0, index=days)
    return pd.DataFrame({"this_month": cur, "typical": typical})


def budget_status(con: sqlite3.Connection, df: pd.DataFrame, today: date) -> pd.DataFrame:
    """This month's spending per top-level category vs budget, with a linear pro-rata pace."""
    b = pd.read_sql_query("SELECT category, monthly_eur AS budget FROM budgets", con)
    if b.empty:
        return b
    this = pd.Timestamp(today.replace(day=1))
    s = spending(df)
    spent = (-s[s.month == this].groupby("top").amount.sum()).rename("spent")
    out = b.merge(spent, left_on="category", right_index=True, how="left").fillna({"spent": 0.0})
    days_in = (this + pd.offsets.MonthEnd(0)).day
    out["used"] = out.spent / out.budget
    out["pace"] = today.day / days_in  # share of the month gone
    out["left"] = out.budget - out.spent
    return out.sort_values("used", ascending=False)


def suggested_budgets(df: pd.DataFrame, today: date, months: int = 6) -> pd.Series:
    this = pd.Timestamp(today.replace(day=1))
    s = spending(df)
    s = s[(s.month < this) & (s.month >= this - pd.DateOffset(months=months))]
    return (-s.groupby("top").amount.sum() / months).round(-1).sort_values(ascending=False)
