"""Static, self-contained HTML snapshot of the dashboard (works offline, phone-friendly).

Plotly's JS is inlined once by default (~4.5 MB) so the file opens anywhere without internet;
`cdn=True` makes it ~200 KB but needs a connection to draw the charts.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go

from . import metrics, viz
from .insights import recurring
from .invest import portfolio

INK = viz.INK["light"]


def _eur(x, d=0, sign=False) -> str:
    if x is None or pd.isna(x):
        return "–"
    s = f"{x:+,.{d}f}" if sign else f"{x:,.{d}f}"
    return f"€{s}" if not s.startswith(("-", "+")) else f"{s[0]}€{s[1:]}"


def _slot(i: int) -> str:
    return viz.SERIES["light"][(i - 1) % 8]


class _Charts:
    def __init__(self, cdn: bool):
        self.first, self.cdn = True, cdn

    def __call__(self, fig: go.Figure, height: int = 300) -> str:
        viz.style(fig, "light", height)
        inc = ("cdn" if self.cdn else True) if self.first else False
        self.first = False
        return fig.to_html(full_html=False, include_plotlyjs=inc,
                           config={"displayModeBar": False, "responsive": True})


def _table(df: pd.DataFrame, money: tuple[str, ...] = (), pct: tuple[str, ...] = ()) -> str:
    rows = []
    for r in df.itertuples(index=False):
        cells = []
        for col, v in zip(df.columns, r):
            if col in money:
                cells.append(f'<td class="num">{_eur(v, 2)}</td>')
            elif col in pct:
                cells.append(f'<td class="num">{"–" if pd.isna(v) else f"{v:.0%}"}</td>')
            else:
                cells.append(f"<td>{html.escape(str(v))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def build(con: sqlite3.Connection, f: metrics.Filters, cdn: bool = False) -> str:
    chart = _Charts(cdn)
    today = date.today()
    df = metrics.transactions(con, f)
    cf = metrics.monthly_cashflow(df)
    tot = metrics.period_totals(df)
    nw = metrics.networth_series(con, f)
    inv = portfolio.summary(con)
    month = metrics.period_totals(metrics.transactions(con, metrics.Filters(
        start=today.replace(day=1), end=today, accounts=f.accounts, joint=f.joint)))

    tiles = [
        ("Net worth", _eur(nw.networth.iloc[-1]) if not nw.empty else "–", ""),
        ("Cash", _eur(nw.cash.iloc[-1]) if not nw.empty else "–", ""),
        ("Investments", _eur(inv["value_eur"]), f"{_eur(inv['unrealised_eur'], sign=True)} ({inv['unrealised_pct']:+.1%})"),
        ("Savings rate (period)", "–" if tot["savings_rate"] is None else f"{tot['savings_rate']:.0%}",
         f"{_eur(tot['income'])} in · {_eur(tot['expenses'])} out"),
        (f"{today:%B} so far", _eur(month["net"], sign=True), f"{_eur(month['income'])} in · {_eur(month['expenses'])} out"),
    ]
    parts = ['<div class="tiles">' + "".join(
        f'<div class="tile"><div class="lbl">{html.escape(a)}</div><div class="val">{b}</div>'
        f'<div class="sub">{html.escape(c)}</div></div>' for a, b, c in tiles) + "</div>"]

    if not nw.empty:
        w = nw.resample("W").last()
        fig = go.Figure()
        for col, name, i in (("investments", "Investments", 1), ("cash", "Cash", 3)):
            fig.add_scatter(x=w.index, y=w[col], name=name, stackgroup="nw", mode="lines",
                            line=dict(width=2, color=_slot(i)), hovertemplate="%{y:,.0f} €")
        parts.append("<h2>Net worth</h2>" + chart(fig, 300))

    if not cf.empty:
        fig = go.Figure()
        fig.add_bar(x=cf.index, y=cf.income, name="Income", marker_color=_slot(1), hovertemplate="%{y:,.0f} €")
        fig.add_bar(x=cf.index, y=cf.expenses, name="Expenses", marker_color=_slot(2), hovertemplate="%{y:,.0f} €")
        fig.update_xaxes(tickformat="%b %y")
        parts.append("<h2>Income vs expenses</h2>" + chart(fig, 280))
        full = cf[(cf.income > 0) & (cf.index < pd.Timestamp(today.replace(day=1)))]
        if not full.empty:
            fig = go.Figure(go.Scatter(x=full.index, y=full.savings_rate, mode="lines+markers", name="Savings rate",
                                       line=dict(width=2, color=_slot(1)), hovertemplate="%{y:.0%}"))
            viz.style(fig, "light", 240, legend=False, yfmt=".0%")
            fig.update_yaxes(range=[max(min(full.savings_rate.min(), 0) - 0.05, -1), 1])
            parts.append("<h2>Savings rate (complete months)</h2>" + chart(fig, 240))

    cats = metrics.by_category(df)
    if not cats.empty:
        d = cats.head(12).iloc[::-1]
        fig = go.Figure(go.Bar(x=d.spent, y=d.name, orientation="h", marker_color=_slot(1),
                               text=[_eur(v) for v in d.spent], textposition="outside", cliponaxis=False,
                               hovertemplate="%{y}: %{x:,.0f} €<extra></extra>"))
        fig.update_layout(margin=dict(r=70))
        fig.update_xaxes(visible=False)
        parts.append("<h2>Spending by category</h2>" + chart(fig, 30 * len(d) + 60))

    pos = pd.DataFrame([{"Position": p.name, "Units": f"{p.quantity:g}", "Value": p.value_eur, "Cost": p.cost_eur,
                         "Gain": p.unrealised_eur} for p in inv["positions"]])
    if not pos.empty:
        parts.append(f"<h2>Investments · {_eur(inv['value_eur'])}</h2>" + _table(pos, money=("Value", "Cost", "Gain")))

    full_df = metrics.transactions(con)
    full_df["amount"] = full_df.my_amount_eur_cents / 100
    rec = recurring.to_frame(recurring.apply_overrides(con, recurring.detect(full_df)))
    if not rec.empty:
        act = rec[rec.status == "active"].sort_values("annual_cost", ascending=False)
        parts.append(f"<h2>Recurring · {_eur(act.monthly_cost.sum())}/month</h2>" + _table(
            act[["merchant", "frequency", "typical", "annual_cost", "next_expected"]].rename(columns={
                "typical": "Amount", "annual_cost": "Per year", "next_expected": "Next"}),
            money=("Amount", "Per year")))

    tm = metrics.top_merchants(df, 10)
    if not tm.empty:
        parts.append("<h2>Top merchants</h2>" + _table(tm[["merchant", "category", "n", "spent"]], money=("spent",)))

    al = con.execute("SELECT title, detail FROM alerts WHERE status = 'new' ORDER BY id DESC LIMIT 15").fetchall()
    if al:
        parts.append("<h2>Open alerts</h2><ul class='alerts'>" + "".join(
            f"<li><b>{html.escape(a['title'])}</b><br><span>{html.escape(a['detail'] or '')}</span></li>" for a in al)
            + "</ul>")

    return _page(f"fin · {today:%d %b %Y}", f"{f.start:%d %b %Y} → {f.end:%d %b %Y} · generated "
                 f"{datetime.now():%d %b %Y %H:%M}", "\n".join(parts))


def _page(title: str, subtitle: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --surface:{INK['surface']}; --ink:{INK['primary']}; --ink2:{INK['secondary']}; --muted:{INK['muted']};
        --grid:{INK['grid']}; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:16px; background:var(--surface); color:var(--ink);
       font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; max-width:1100px; margin-inline:auto; }}
h1 {{ font-size:22px; margin:4px 0 0; }} .subtitle {{ color:var(--muted); margin-bottom:16px; font-size:13px; }}
h2 {{ font-size:16px; margin:28px 0 6px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; }}
.tile {{ border:1px solid var(--grid); border-radius:10px; padding:10px 12px; }}
.lbl {{ color:var(--ink2); font-size:13px; }} .val {{ font-size:22px; font-weight:600; }}
.sub {{ color:var(--muted); font-size:12px; }}
.tw {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); white-space:nowrap; }}
th {{ color:var(--ink2); font-weight:600; }} td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.alerts {{ padding-left:18px; }} .alerts span {{ color:var(--muted); font-size:13px; }}
.foot {{ color:var(--muted); font-size:12px; margin-top:28px; }}
</style></head><body>
<h1>{html.escape(title)}</h1><div class="subtitle">{html.escape(subtitle)}</div>
{body}
<div class="foot">Personal financial data. Generated locally by fin.</div>
</body></html>"""
