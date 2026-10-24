"""Validated chart palette + Plotly styling shared by the app and the static HTML report.

Categorical slots are used in fixed order (colour follows the entity, never its rank); light and
dark are separately stepped. Validated with the dataviz skill's validator (both modes pass).
"""

from __future__ import annotations

import plotly.graph_objects as go

SERIES = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
}
INK = {
    "light": {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "axis": "#c3c2b7", "surface": "#fcfcfb", "other": "#b4b2a9", "neg": "#e34948", "pos": "#2a78d6"},
    "dark": {"primary": "#f0efec", "secondary": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "axis": "#383835", "surface": "#1a1a19", "other": "#5f5e5a", "neg": "#e66767", "pos": "#3987e5"},
}
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
SEQ_BLUE_DARK = ["#1f2b3a", "#184f95", "#1c5cab", "#2a78d6", "#3987e5", "#6da7ec", "#b7d3f6"]


def style(fig: go.Figure, mode: str = "light", height: int = 320, legend: bool = True,
          yfmt: str | None = None) -> go.Figure:
    ink = INK[mode]
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif', color=ink["secondary"], size=12),
        hoverlabel=dict(font=dict(family="system-ui, sans-serif")),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, title=None,
                    font=dict(color=ink["secondary"])),
        bargap=0.3, bargroupgap=0.08, barcornerradius=4,
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=False, linecolor=ink["axis"], tickfont=dict(color=ink["muted"]), title=None,
                     zeroline=False, automargin=True)
    fig.update_yaxes(gridcolor=ink["grid"], gridwidth=1, zeroline=True, zerolinecolor=ink["axis"],
                     tickfont=dict(color=ink["muted"]), title=None, tickformat=yfmt, automargin=True)
    return fig
