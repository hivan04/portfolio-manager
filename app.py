"""Maple — portfolio dashboard.

    .venv/bin/streamlit run app.py

Five views: Overview, Holdings, Rules, Market (top 5 live), Advisor.
Portfolio data comes from data/portfolio.json, written by ``t212.sync``.
"""

from __future__ import annotations

import sys
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "statarb-models"))

import analytics
import prompts
from memory import Memory
from prices import (
    Quote,
    get_history,
    load_portfolio,
    market_context,
    quote_portfolio,
    t212_to_yahoo,
    top_positions,
)

GOLD = "#C9A227"
GREEN = "#3FB27F"
RED = "#E05260"
MUTED = "#8B94A7"
CARD = "#141922"
LINE = "#232A36"
# Palette for the currency pie, coolest to warmest.
PIE_COLOURS = ["#7FC5E8", "#4A7FB5", "#C9A227", "#3FB27F", "#8B6FB0", "#E0876A"]

st.set_page_config(
    page_title="Maple",
    page_icon="🍁",
    layout="wide",
    initial_sidebar_state="collapsed",
)

STYLE = f"""
<style>
  #MainMenu, footer, header {{ visibility: hidden; }}

  .stApp {{
    background:
      radial-gradient(1100px 600px at 12% -8%, #161C28 0%, transparent 60%),
      radial-gradient(900px 500px at 88% 4%, #12202B 0%, transparent 55%),
      #0B0E14;
  }}

  .block-container {{ padding-top: 2.2rem; max-width: 1500px; }}

  h1, h2, h3, h4 {{
    font-family: ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
    letter-spacing: -0.02em;
    font-weight: 620;
  }}

  .masthead {{
    display: flex; align-items: baseline; gap: 0.9rem;
    border-bottom: 1px solid {LINE}; padding-bottom: 1rem; margin-bottom: 1.6rem;
  }}
  .masthead .mark {{ color: {GOLD}; font-size: 1.5rem; line-height: 1; }}
  .masthead .title {{ font-size: 1.5rem; font-weight: 640; color: #E6E9EF; }}
  .masthead .sub {{
    color: {MUTED}; font-size: 0.78rem; margin-left: auto;
    text-transform: uppercase; letter-spacing: 0.1em;
  }}

  .card {{
    background: linear-gradient(180deg, {CARD} 0%, #10141C 100%);
    border: 1px solid {LINE}; border-radius: 14px;
    padding: 1.1rem 1.25rem; height: 100%;
  }}
  .card .label {{
    color: {MUTED}; font-size: 0.7rem; text-transform: uppercase;
    letter-spacing: 0.13em; margin-bottom: 0.5rem;
  }}
  .card .value {{
    font-size: 1.72rem; font-weight: 640; color: #F2F5FA;
    letter-spacing: -0.03em; line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }}
  .card .delta {{ font-size: 0.85rem; margin-top: 0.35rem; font-weight: 560; }}
  .up {{ color: {GREEN}; }} .down {{ color: {RED}; }} .flat {{ color: {MUTED}; }}

  .flag {{
    border-radius: 12px; padding: 0.85rem 1.05rem; margin-bottom: 0.7rem;
    border: 1px solid {LINE}; background: {CARD};
  }}
  .flag.critical {{ border-left: 3px solid {RED}; background: #1B1216; }}
  .flag.warning  {{ border-left: 3px solid {GOLD}; background: #1A1710; }}
  .flag.info     {{ border-left: 3px solid #4A7FB5; background: #101720; }}
  .flag .rule {{
    font-weight: 640; font-size: 0.82rem; letter-spacing: 0.02em;
    margin-bottom: 0.25rem;
  }}
  .flag .detail {{ color: #B9C0CE; font-size: 0.86rem; line-height: 1.55; }}

  .brief {{
    background: {CARD}; border: 1px solid {LINE}; border-left: 3px solid {GOLD};
    border-radius: 12px; padding: 1.3rem 1.5rem; line-height: 1.68;
    font-size: 0.93rem;
  }}

  .stTabs [data-baseweb="tab-list"] {{ gap: 0.35rem; border-bottom: 1px solid {LINE}; }}
  .stTabs [data-baseweb="tab"] {{
    background: transparent; border-radius: 8px 8px 0 0;
    padding: 0.6rem 1.1rem; font-size: 0.87rem; color: {MUTED};
  }}
  .stTabs [aria-selected="true"] {{ color: {GOLD} !important; background: {CARD}; }}

  /* Sidebar removed entirely. */
  [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
  [data-testid="collapsedControl"] {{ display: none !important; }}

  .statusbar {{
    color: {MUTED}; font-size: 0.76rem; padding-top: 0.45rem;
    letter-spacing: 0.01em;
  }}

  [data-testid="stDataFrame"] {{ border: 1px solid {LINE}; border-radius: 10px; }}
  .stButton button {{
    border: 1px solid {LINE}; background: {CARD}; color: #E6E9EF;
    border-radius: 9px; font-size: 0.85rem; font-weight: 560;
  }}
  .stButton button:hover {{ border-color: {GOLD}; color: {GOLD}; }}
  .tiny {{ color: {MUTED}; font-size: 0.76rem; }}
</style>
"""
st.markdown(STYLE, unsafe_allow_html=True)


# ------------------------------------------------------------------ data


@st.cache_data(ttl=120)
def _portfolio() -> dict:
    return load_portfolio()


@st.cache_data(ttl=300)
def _quotes(tickers: tuple[str, ...]) -> dict:
    holdings = [{"Ticker": t, "Name": t} for t in tickers]
    return {k: v.as_dict() for k, v in quote_portfolio(holdings).items()}


@st.cache_data(ttl=300)
def _market() -> dict:
    return {k: v.as_dict() for k, v in market_context().items()}


@st.cache_data(ttl=900)
def _history(symbol: str, period: str = "6mo"):
    return get_history(symbol, period=period)


def money(value: float | None, symbol: str = "£") -> str:
    if value is None:
        return "—"
    return f"{symbol}{value:,.0f}" if abs(value) >= 1000 else f"{symbol}{value:,.2f}"


def card(label: str, value: str, delta: str = "", tone: str = "flat") -> str:
    delta_html = f'<div class="delta {tone}">{delta}</div>' if delta else ""
    return (
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value">{value}</div>{delta_html}</div>'
    )


def tone_for(value: float | None) -> str:
    if value is None:
        return "flat"
    return "up" if value > 0 else ("down" if value < 0 else "flat")


def _chart_layout(y_title: str = "", height: int = 300) -> dict:
    """Shared dark-transparent Plotly layout."""
    axis = {
        "gridcolor": LINE,
        "zerolinecolor": LINE,
        "linecolor": LINE,
        "tickfont": {"color": MUTED, "size": 11},
        "title_font": {"color": MUTED, "size": 11},
    }
    return {
        "height": height,
        "margin": {"t": 14, "b": 34, "l": 52, "r": 14},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "showlegend": False,
        "hoverlabel": {"bgcolor": CARD, "bordercolor": LINE,
                       "font": {"color": "#E6E9EF"}},
        "xaxis": dict(axis),
        "yaxis": {**axis, "title_text": y_title},
    }


portfolio = _portfolio()
holdings = portfolio.get("holdings") or []
checks = prompts.diagnostics(portfolio) if holdings else {}
memory = Memory()

if holdings:
    try:
        memory.save_snapshot(portfolio)
    except Exception:  # noqa: BLE001 - never let persistence break the view
        pass


# -------------------------------------------------------------- advisor

try:
    from analyzer import Analyzer

    analyzer = Analyzer(portfolio=portfolio, memory=memory)
    configured = analyzer.is_configured()
except Exception:  # noqa: BLE001
    analyzer, configured = None, False


# -------------------------------------------------------------- masthead

st.markdown(
    f'<div class="masthead"><span class="mark">🍁</span>'
    f'<span class="title">Maple</span>'
    f'<span class="sub">{date.today():%A %d %B %Y}</span></div>',
    unsafe_allow_html=True,
)

# Status strip — what the sidebar used to carry, condensed to one line.
bits: list[str] = []
stale = False

generated = portfolio.get("generatedAt")
if generated:
    try:
        stamp = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        age = (datetime.now(stamp.tzinfo) - stamp).total_seconds() / 3600
        bits.append(f"Synced {stamp:%d %b %H:%M} · {age:.0f}h ago")
        stale = age > 48
    except (ValueError, TypeError):
        bits.append(f"Synced {generated}")
else:
    bits.append("No snapshot")

if configured:
    try:
        resolved = analyzer.resolve_model(analyzer.model)
    except Exception:  # noqa: BLE001
        resolved = analyzer.model
    bits.append(f"Gemini · {resolved}")
else:
    try:
        import google.genai  # noqa: F401

        bits.append("Advisor: no API key")
    except ImportError:
        bits.append("Advisor: google-genai not installed here")

try:
    import telegram_alert

    bits.append("Telegram on" if telegram_alert.is_configured() else "Telegram off")
except Exception:  # noqa: BLE001
    pass

bits.append(f"{len(holdings)} positions")

status_left, status_right = st.columns([6, 1])
with status_left:
    st.markdown(
        f'<div class="statusbar">{" · ".join(bits)}</div>', unsafe_allow_html=True
    )
with status_right:
    if st.button("Refresh", width="stretch"):
        st.cache_data.clear()
        st.rerun()

if stale:
    st.warning("Snapshot is over 48h old — run `python -m t212.sync`.")

if not holdings:
    st.error(
        "No holdings in `data/portfolio.json`. Run `python -m t212.sync` to pull "
        "your Trading 212 account."
    )
    st.stop()

(
    tab_overview,
    tab_holdings,
    tab_analytics,
    tab_dividends,
    tab_statarb,
    tab_rules,
    tab_market,
    tab_advisor,
) = st.tabs(
    [
        "Overview",
        "Holdings",
        "Advanced Analytics",
        "Dividends",
        "Stat Arb",
        "Rules",
        "Market",
        "Advisor",
    ]
)


# -------------------------------------------------------------- overview

with tab_overview:
    c1, c2, c3, c4 = st.columns(4)
    pl = checks["unrealised_pl"] or 0.0
    pct = checks["unrealised_pct"] or 0.0

    with c1:
        st.markdown(
            card("Account value", money(checks["total_value"]),
                 f"cost {money(checks['cost_basis'])}", "flat"),
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            card("Unrealised P/L", money(pl), f"{pct:+.2f}%", tone_for(pl)),
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            card("Realised P/L", money(checks["realised_pl"]), "all time", "flat"),
            unsafe_allow_html=True,
        )
    with c4:
        severity = sum(1 for b in checks["breaches"] if b["severity"] == "critical")
        st.markdown(
            card(
                "Positions",
                str(checks["position_count"]),
                f"{severity} critical flag{'s' if severity != 1 else ''}",
                "down" if severity else "up",
            ),
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)
    left, right = st.columns([3, 2])

    with left:
        st.markdown("##### Concentration")
        ranked = sorted(
            holdings, key=lambda h: float(h.get("Current value") or 0), reverse=True
        )[:12]
        frame = pd.DataFrame(
            {
                "Position": [h["Name"][:26] for h in ranked],
                "Weight %": [float(h.get("Weight %") or 0) * 100 for h in ranked],
            }
        ).set_index("Position")
        st.bar_chart(frame, height=340, color=GOLD)
        st.markdown(
            f'<div class="tiny">Top 5 = {checks["top5_pct"]:.1f}% · '
            f'Top 10 = {checks["top10_pct"]:.1f}% · '
            f'{checks["tail_count"]} positions under 0.5% hold '
            f'{checks["tail_pct"]:.1f}% of value</div>',
            unsafe_allow_html=True,
        )

    with right:
        st.markdown("##### Currency exposure")
        mix = sorted(checks["currency_mix"].items(), key=lambda kv: -kv[1])

        figure = go.Figure(
            go.Pie(
                labels=[code for code, _ in mix],
                values=[pct for _, pct in mix],
                hole=0.58,
                sort=False,
                direction="clockwise",
                marker={
                    "colors": PIE_COLOURS[: len(mix)],
                    "line": {"color": "#0B0E14", "width": 2},
                },
                textinfo="label+percent",
                textposition="outside",
                textfont={"size": 12, "color": "#C6CDD9"},
                hovertemplate="%{label}: %{value:.1f}%<extra></extra>",
            )
        )
        figure.update_layout(
            height=340,
            margin={"t": 10, "b": 10, "l": 10, "r": 10},
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False,
            annotations=[
                {
                    "text": f"<b>{mix[0][0]}</b><br><span style='font-size:11px'>"
                    f"{mix[0][1]:.0f}%</span>",
                    "x": 0.5,
                    "y": 0.5,
                    "font": {"size": 17, "color": "#E6E9EF"},
                    "showarrow": False,
                }
            ],
        )
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
        st.markdown(
            '<div class="tiny">Reporting currency is GBP; living costs will be '
            'HKD, which is pegged to USD. The real exposure is GBP/USD.</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("##### Morning brief")

    if analyzer is None:
        st.info("Advisor module unavailable.")
    else:
        col_a, col_b, _ = st.columns([1, 1, 3])
        with col_a:
            generate = st.button(
                "Generate brief" if configured else "Generate (offline)",
                width="stretch",
            )
        with col_b:
            offline_only = st.button("Offline brief", width="stretch") if configured else False

        if generate or offline_only:
            with st.spinner("Reading the book…"):
                response = analyzer.morning_brief(offline=bool(offline_only))
            if response.ok:
                st.markdown(
                    f'<div class="brief">{response.text}</div>',
                    unsafe_allow_html=True,
                )
                if response.model == "offline":
                    st.markdown(
                        '<div class="tiny">Composed from the rule engine — '
                        'no model call.</div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.error(response.error)
        else:
            previous = memory.briefs(limit=1)
            if previous:
                st.markdown(
                    f'<div class="brief">{previous[0]["body"]}</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="tiny">Last brief · {previous[0]["brief_date"]} · '
                    f'{previous[0]["channel"]}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="tiny">No brief yet. Generate one — it works '
                    'without an API key.</div>',
                    unsafe_allow_html=True,
                )


# -------------------------------------------------------------- holdings

with tab_holdings:
    st.markdown("##### All positions")

    frame = pd.DataFrame(holdings)
    frame["Weight %"] = frame["Weight %"] * 100
    frame["Unrealised P/L %"] = frame["Unrealised P/L %"] * 100
    frame["Type"] = frame.apply(
        lambda row: "Fund"
        if (row.get("ISIN") or "")[:2] in prompts.FUND_DOMICILES
        and row.get("Ticker") not in prompts.NOT_A_FUND
        else "Direct",
        axis=1,
    )

    view = frame[
        [
            "Name", "Ticker", "Current value", "Weight %",
            "Unrealised P/L", "Unrealised P/L %", "Instrument currency",
            "Opened", "Type",
        ]
    ].rename(columns={"Instrument currency": "Ccy", "Current value": "Value"})

    filters = st.columns([1, 1, 1, 2])
    with filters[0]:
        losers_only = st.checkbox("Losers only")
    with filters[1]:
        funds_only = st.checkbox("Funds only")
    with filters[2]:
        hide_tail = st.checkbox("Hide sub-0.5%")
    with filters[3]:
        search = st.text_input("Search", placeholder="name or ticker", label_visibility="collapsed")

    if losers_only:
        view = view[view["Unrealised P/L %"] < 0]
    if funds_only:
        view = view[view["Type"] == "Fund"]
    if hide_tail:
        view = view[view["Weight %"] >= 0.5]
    if search:
        mask = view["Name"].str.contains(search, case=False, na=False) | view[
            "Ticker"
        ].str.contains(search, case=False, na=False)
        view = view[mask]

    st.dataframe(
        view.style.format(
            {
                "Value": "£{:,.0f}",
                "Weight %": "{:.2f}%",
                "Unrealised P/L": "£{:,.0f}",
                "Unrealised P/L %": "{:+.1f}%",
            }
        ).map(
            lambda v: f"color: {GREEN}" if isinstance(v, (int, float)) and v > 0
            else (f"color: {RED}" if isinstance(v, (int, float)) and v < 0 else ""),
            subset=["Unrealised P/L", "Unrealised P/L %"],
        ),
        width="stretch",
        height=620,
        hide_index=True,
    )
    st.markdown(
        f'<div class="tiny">{len(view)} of {len(frame)} positions shown</div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------- analytics


@st.cache_data(ttl=900, show_spinner=False)
def _series(tickers_and_values: tuple, period: str):
    rebuilt = [{"Ticker": t, "Current value": v} for t, v in tickers_and_values]
    return analytics.portfolio_series(rebuilt, period=period)


with tab_analytics:
    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.markdown("##### Risk & performance")
    with head_r:
        period = st.selectbox(
            "Lookback",
            ["6mo", "1y", "2y", "5y"],
            index=1,
            label_visibility="collapsed",
        )

    key = tuple(
        (h["Ticker"], float(h.get("Current value") or 0.0)) for h in holdings
    )
    with st.spinner("Rebuilding the value series…"):
        series = _series(key, period)

    if series.empty:
        st.warning(
            "Could not build enough price history to compute metrics. "
            "This usually means yfinance is rate-limiting — try Refresh in a minute."
        )
    else:
        metrics = analytics.risk_metrics(series, period=period)

        def metric_card(label: str, value: str, sub: str, tone: str = "flat") -> str:
            return card(label, value, sub, tone)

        row1 = st.columns(3)
        with row1[0]:
            st.markdown(
                metric_card(
                    "Sharpe ratio",
                    f"{metrics.sharpe:.2f}" if metrics.sharpe is not None else "—",
                    "excess return per unit of risk",
                    "up" if (metrics.sharpe or 0) > 1 else "flat",
                ),
                unsafe_allow_html=True,
            )
        with row1[1]:
            st.markdown(
                metric_card(
                    "Max drawdown",
                    f"{metrics.max_drawdown * 100:.1f}%"
                    if metrics.max_drawdown is not None
                    else "—",
                    "worst peak-to-trough",
                    "down",
                ),
                unsafe_allow_html=True,
            )
        with row1[2]:
            st.markdown(
                metric_card(
                    "CAGR",
                    f"{metrics.cagr * 100:.1f}%" if metrics.cagr is not None else "—",
                    "annualised over the window",
                    tone_for(metrics.cagr),
                ),
                unsafe_allow_html=True,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        row2 = st.columns(3)
        with row2[0]:
            st.markdown(
                metric_card(
                    "Volatility (ann.)",
                    f"{metrics.volatility * 100:.1f}%"
                    if metrics.volatility is not None
                    else "—",
                    "standard deviation of returns",
                    "flat",
                ),
                unsafe_allow_html=True,
            )
        with row2[1]:
            st.markdown(
                metric_card(
                    "Beta (vs S&P 500)",
                    f"{metrics.beta:.2f}" if metrics.beta is not None else "—",
                    "1.0 = moves with the index",
                    "flat",
                ),
                unsafe_allow_html=True,
            )
        with row2[2]:
            st.markdown(
                metric_card(
                    "VaR 95% (daily)",
                    f"{metrics.var95 * 100:.2f}%"
                    if metrics.var95 is not None
                    else "—",
                    f"≈ {money(metrics.var95_value)} on a bad day"
                    if metrics.var95_value is not None
                    else "1-in-20 daily loss",
                    "down",
                ),
                unsafe_allow_html=True,
            )

        st.markdown("<br>", unsafe_allow_html=True)

        # Side by side, per request — value on the left, drawdown on the right.
        chart_l, chart_r = st.columns(2)

        with chart_l:
            st.markdown(f"**Estimated portfolio value — {period}**")
            value_fig = go.Figure(
                go.Scatter(
                    x=series.index,
                    y=series.values,
                    mode="lines",
                    line={"color": "#4EA8DE", "width": 2},
                    fill="tozeroy",
                    fillcolor="rgba(78,168,222,0.10)",
                    hovertemplate="%{x|%d %b %Y}<br>£%{y:,.0f}<extra></extra>",
                )
            )
            value_fig.update_layout(**_chart_layout("Approx. value"))
            st.plotly_chart(
                value_fig, width="stretch", config={"displayModeBar": False}
            )

        with chart_r:
            st.markdown("**Drawdown from peak**")
            drawdowns = analytics.drawdown_series(series) * 100.0
            dd_fig = go.Figure(
                go.Scatter(
                    x=drawdowns.index,
                    y=drawdowns.values,
                    mode="lines",
                    line={"color": RED, "width": 1.6},
                    fill="tozeroy",
                    fillcolor="rgba(224,82,96,0.16)",
                    hovertemplate="%{x|%d %b %Y}<br>%{y:.2f}%<extra></extra>",
                )
            )
            dd_fig.update_layout(**_chart_layout("Drawdown (%)"))
            st.plotly_chart(
                dd_fig, width="stretch", config={"displayModeBar": False}
            )

        # Show the window actually used. Holdings that listed part-way
        # through are grossed up, and dates below 55% coverage are dropped,
        # so the effective window can be shorter than requested.
        span_days = (series.index[-1] - series.index[0]).days
        coverage = series.attrs.get("coverage", 1.0)
        priced = series.attrs.get("priced", 0)
        missing = series.attrs.get("missing", [])

        detail = (
            f"Window used: {series.index[0]:%d %b %Y} → {series.index[-1]:%d %b %Y} "
            f"({span_days} days, {metrics.observations} trading days). "
            f"{priced} of {len(holdings)} positions priced; "
            f"{coverage:.0%} of book value covered at the start of the window."
        )
        if missing:
            detail += f" No history for {len(missing)}: {', '.join(missing[:6])}"
            if len(missing) > 6:
                detail += f" +{len(missing) - 6} more"
        if coverage < 0.9:
            detail += (
                " Holdings that listed mid-window are grossed up from those that "
                "existed — early figures are the roughest."
            )

        st.markdown(f'<div class="tiny">{detail}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="tiny">⚠️ Metrics assume current holdings were held for the '
            'full window. Interim buys and sells are not modelled and FX moves are '
            'excluded — directional estimates, not a performance record. '
            'Sharpe uses a 4% risk-free rate.</div>',
            unsafe_allow_html=True,
        )


# ------------------------------------------------------------- dividends

with tab_dividends:
    dividends = analytics.load_dividends()

    if dividends.empty:
        st.info(
            "No dividend data yet. Run `python -m t212.sync` — it now pulls your "
            "payment history from Trading 212 into `data/dividends.json`."
        )
    else:
        summary = analytics.dividend_summary(dividends)

        div_row = st.columns(3)
        with div_row[0]:
            st.markdown(
                card("Total dividends", f"£{summary['total']:,.2f}",
                     f"{summary['payments']} payments from {summary['payers']} holdings",
                     "up"),
                unsafe_allow_html=True,
            )
        with div_row[1]:
            st.markdown(
                card("YTD dividends", f"£{summary['ytd']:,.2f}",
                     f"calendar {date.today().year}", "up"),
                unsafe_allow_html=True,
            )
        with div_row[2]:
            book = checks.get("invested_value") or 0.0
            yield_pct = analytics.yield_on_book(dividends, book)
            st.markdown(
                card("Avg monthly", f"£{summary['avg_monthly']:,.2f}",
                     f"trailing 12m £{summary['ttm']:,.0f}"
                     + (f" · {yield_pct:.2f}% yield on book" if yield_pct else ""),
                     "flat"),
                unsafe_allow_html=True,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("**Monthly dividends received**")

        monthly = analytics.monthly_dividends(dividends)
        bar_fig = go.Figure(
            go.Bar(
                x=monthly["Month"],
                y=monthly["Amount"],
                marker={"color": GREEN, "line": {"width": 0}},
                hovertemplate="%{x|%b %Y}<br>£%{y:,.2f}<extra></extra>",
            )
        )
        bar_fig.update_layout(**_chart_layout("Dividends (£)", height=330))
        bar_fig.update_xaxes(title_text="Month")
        st.plotly_chart(
            bar_fig, width="stretch", config={"displayModeBar": False}
        )

        left, right = st.columns([3, 2])

        with left:
            st.markdown("**All payments**")
            table = dividends.copy()
            table["Paid On"] = table["Paid on"].dt.strftime("%d %b %Y")
            table = table.rename(
                columns={"Amount": "Amount (GBP)", "Quantity": "Qty",
                         "Per share": "Per Share"}
            )
            columns = [
                c
                for c in ["Name", "Ticker", "Paid On", "Amount (GBP)", "Qty",
                          "Per Share", "Currency"]
                if c in table
            ]
            st.dataframe(
                table[columns].style.format(
                    {"Amount (GBP)": "£{:,.2f}", "Qty": "{:,.4f}",
                     "Per Share": "{:,.2f}"}
                ),
                width="stretch",
                height=430,
                hide_index=True,
            )

        with right:
            st.markdown("**Top payers**")
            payers = analytics.dividends_by_holding(dividends, limit=12)
            st.dataframe(
                payers.style.format({"Amount": "£{:,.2f}"}),
                width="stretch",
                height=430,
                hide_index=True,
            )


# --------------------------------------------------------------- stat arb


def _scan(request: tuple, progress):
    """Run a scan, caching by request in session state.

    Deliberately not @st.cache_data — that would swallow the progress
    callback, and the screen is slow enough that watching it matters.
    """
    import statarb

    cache = st.session_state.setdefault("statarb_cache", {})
    if request in cache:
        return cache[request]

    uni, start_date, cap_n, alpha_v, cost_v, bands_v, pass_v = request
    result = statarb.run_scan(
        uni, start=start_date, top_n=5, max_tickers=cap_n,
        coint_alpha=alpha_v, cost_bps=cost_v, optimise_bands=bands_v,
        min_pass_rate=pass_v, progress=progress,
    )
    cache[request] = result
    return result


SIGNAL_COLOUR = {
    "LONG SPREAD": GREEN,
    "SHORT SPREAD": GOLD,
    "FLAT": MUTED,
    "WAIT": MUTED,
}

with tab_statarb:
    st.markdown("##### Statistical arbitrage — cointegrated pairs")
    st.markdown(
        '<div class="tiny">Engle-Granger screen with rolling stability, Kalman-filter '
        'hedge ratios, walk-forward backtest. Entry/exit bands are fitted on the '
        'screening sample only; every headline figure comes from folds the fit never '
        'saw.</div>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    form = st.columns([3, 1.2, 1, 1])
    with form[0]:
        universe_input = st.text_input(
            "Universe",
            value="nasdaq100",
            help=(
                "An index — sp500, nasdaq100, ftse100, dax, hsi, sti, nikkei225 — "
                "or two or more Yahoo tickers, e.g. 'KO PEP' or '0700.HK 9988.HK'."
            ),
            label_visibility="collapsed",
            placeholder="nasdaq100  ·  or  KO PEP MCD",
        )
    with form[1]:
        start_year = st.selectbox(
            "From", ["2015-01-01", "2018-01-01", "2020-01-01", "2022-01-01"],
            index=1, label_visibility="collapsed",
        )
    with form[2]:
        cap = st.number_input(
            "Max tickers", min_value=4, max_value=200, value=30, step=5,
            label_visibility="collapsed", help="Universe cap — the screen is O(n²).",
        )
    with form[3]:
        run_scan_now = st.button("Find pairs", width="stretch", type="primary")

    # Up-front cost estimate — the screen grows with the square of the universe.
    kind, values = __import__("statarb").parse_universe(universe_input)
    if kind == "index":
        n_est = int(cap)
        source = f"{values[0].upper()} capped at {n_est}"
    else:
        n_est = len(values)
        source = f"{n_est} ticker{'s' if n_est != 1 else ''}"
    combos = n_est * (n_est - 1) // 2
    minutes = max(0.5, combos * 0.09 / 60 + n_est * 0.8 / 60)
    st.markdown(
        f'<div class="tiny">{source} → <b>{combos:,} pairs</b> to test · '
        f'roughly {minutes:.0f}–{minutes * 2:.0f} min on the first run, '
        f'then cached.</div>',
        unsafe_allow_html=True,
    )

    with st.expander("Advanced settings"):
        adv = st.columns(4)
        with adv[0]:
            alpha = st.slider("Cointegration p-value", 0.01, 0.10, 0.05, 0.01)
        with adv[1]:
            pass_rate = st.slider(
                "Min stability", 0.0, 0.6, 0.30, 0.05,
                help=(
                    "Fraction of rolling windows in which the pair must still "
                    "cointegrate. Genuine pairs score 35–45% — a 50% bar rejects "
                    "almost everything."
                ),
            )
        with adv[2]:
            costs = st.slider("Cost per leg (bps)", 0.0, 50.0, 15.0, 2.5)
        with adv[3]:
            fit_bands = st.checkbox(
                "Fit entry/exit bands", value=True,
                help="Grid search on the screening sample. Unticked uses 1.5 / 0.5.",
            )

    if run_scan_now:
        st.session_state.statarb_request = (
            universe_input, start_year, int(cap), float(alpha),
            float(costs), fit_bands, float(pass_rate),
        )

    request = st.session_state.get("statarb_request")

    if not request:
        st.info(
            "Enter a universe and press **Find pairs**. Any Yahoo Finance ticker "
            "works — two or more of them, or an index name. An index scan tests "
            "every combination, so the first run takes a few minutes; results are "
            "cached for the session."
        )
    else:
        bar = st.progress(0.0, text="Starting…")
        stage = st.empty()

        def report(fraction: float, message: str) -> None:
            bar.progress(min(max(fraction, 0.0), 1.0), text=message)
            stage.markdown(
                f'<div class="tiny">{message}</div>', unsafe_allow_html=True
            )

        try:
            scan = _scan(request, report)
        # BaseException so a SystemExit from a missing optional dependency
        # surfaces as an error rather than freezing the page mid-render.
        except BaseException as error:  # noqa: BLE001
            bar.empty()
            stage.empty()
            st.error(f"Scan failed: {type(error).__name__}: {error}")
            if isinstance(error, (ImportError, ModuleNotFoundError, SystemExit)):
                import sys

                st.code(
                    f"{sys.executable} -m pip install statsmodels lxml",
                    language="bash",
                )
            scan = None

        if scan is not None:
            bar.empty()
            stage.empty()

            # Funnel — always shown, so an empty result is diagnosable.
            with st.expander(
                f"Screening funnel — {scan.funnel.cointegrated} cointegrated of "
                f"{scan.funnel.pairs_tested:,} pairs tested",
                expanded=bool(scan.error),
            ):
                funnel_frame = pd.DataFrame(
                    scan.funnel.as_rows(), columns=["Stage", "Count"]
                )
                st.dataframe(
                    funnel_frame, width="stretch", hide_index=True
                )
                if scan.funnel.relaxed:
                    st.caption(
                        "Stability bar not met by any pair — the most stable "
                        "candidates were kept instead."
                    )

            for note in scan.notes:
                st.warning(note)

            if scan.error:
                st.warning(scan.error)
            elif not scan.pairs:
                st.warning("No tradeable pairs found.")
            else:
                st.markdown(
                    f'<div class="tiny">{len(scan.universe)} tickers · '
                    f'{scan.n_candidates} cointegrated · {scan.n_stable} stable · '
                    f'showing top {len(scan.pairs)} by walk-forward Sharpe</div>',
                    unsafe_allow_html=True,
                )
                st.markdown("<br>", unsafe_allow_html=True)

                # --- ranked table ------------------------------------
                table = pd.DataFrame([p.as_row() for p in scan.pairs])
                st.dataframe(
                    table.style.format(
                        {
                            "Sharpe": "{:.2f}",
                            "Return": "{:+.1%}",
                            "Ann. return": "{:+.1%}",
                            "Max DD": "{:.1%}",
                            "Entry z": "±{:.2f}",
                            "Exit z": "±{:.2f}",
                            "Current z": "{:+.2f}",
                            "Coint p": "{:.4f}",
                            "Stability": "{:.0%}",
                            "Half-life": "{:.0f}d",
                            "Trades": "{:.0f}",
                        }
                    ).map(
                        lambda v: f"color: {GREEN}" if isinstance(v, (int, float)) and v > 0
                        else (f"color: {RED}" if isinstance(v, (int, float)) and v < 0 else ""),
                        subset=["Sharpe", "Return", "Ann. return"],
                    ),
                    width="stretch",
                    hide_index=True,
                )

                # --- recommendations ---------------------------------
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown("**Where each pair sits today**")
                for pair in scan.pairs:
                    colour = SIGNAL_COLOUR.get(pair.signal, MUTED)
                    actionable = pair.signal in ("LONG SPREAD", "SHORT SPREAD")
                    st.markdown(
                        f'<div class="flag" style="border-left:3px solid {colour};">'
                        f'<div class="rule">{pair.name} — '
                        f'<span style="color:{colour}">{pair.signal}</span>'
                        f'{"  ◂ actionable" if actionable else ""}</div>'
                        f'<div class="detail">{pair.recommendation}</div>'
                        f'<div class="tiny" style="margin-top:0.4rem;">'
                        f'Walk-forward Sharpe {pair.sharpe:.2f} · '
                        f'in-sample band fit {pair.band_sharpe_is:.2f} · '
                        f'{pair.n_trades:.0f} trades over {pair.days} days'
                        f'</div></div>',
                        unsafe_allow_html=True,
                    )

                # --- portfolio charts --------------------------------
                if not scan.portfolio.empty:
                    st.markdown("<br>", unsafe_allow_html=True)
                    st.markdown("**Equal-weight book of the top pairs**")

                    metrics = scan.portfolio_metrics
                    mcols = st.columns(5)
                    for column, (label, value, fmt) in zip(
                        mcols,
                        [
                            ("Sharpe", metrics.get("sharpe"), "{:.2f}"),
                            ("Total return", metrics.get("total_return"), "{:+.1%}"),
                            ("Ann. return", metrics.get("ann_return"), "{:+.1%}"),
                            ("Max drawdown", metrics.get("max_drawdown"), "{:.1%}"),
                            ("Hit rate", metrics.get("hit_rate"), "{:.1%}"),
                        ],
                    ):
                        with column:
                            shown = fmt.format(value) if value is not None else "—"
                            st.metric(label, shown)

                    equity = (1 + scan.portfolio.fillna(0)).cumprod() * 100
                    drawdown = (equity / equity.cummax() - 1) * 100

                    chart_a, chart_b = st.columns(2)
                    with chart_a:
                        st.markdown("**Cumulative PnL (base 100)**")
                        eq_fig = go.Figure(
                            go.Scatter(
                                x=equity.index, y=equity.values, mode="lines",
                                line={"color": GOLD, "width": 2},
                                fill="tozeroy",
                                fillcolor="rgba(201,162,39,0.10)",
                                hovertemplate="%{x|%d %b %Y}<br>%{y:,.1f}<extra></extra>",
                            )
                        )
                        eq_fig.update_layout(**_chart_layout("Index (100 = start)"))
                        if scan.screen_end is not None:
                            eq_fig.add_vline(
                                x=scan.screen_end, line_dash="dash",
                                line_color=MUTED, line_width=1,
                                annotation_text="end of screening sample",
                                annotation_font_size=10,
                                annotation_font_color=MUTED,
                            )
                        st.plotly_chart(
                            eq_fig, width="stretch",
                            config={"displayModeBar": False},
                        )

                    with chart_b:
                        st.markdown("**Drawdown**")
                        dd_fig = go.Figure(
                            go.Scatter(
                                x=drawdown.index, y=drawdown.values, mode="lines",
                                line={"color": RED, "width": 1.6},
                                fill="tozeroy",
                                fillcolor="rgba(224,82,96,0.16)",
                                hovertemplate="%{x|%d %b %Y}<br>%{y:.2f}%<extra></extra>",
                            )
                        )
                        dd_fig.update_layout(**_chart_layout("Drawdown (%)"))
                        st.plotly_chart(
                            dd_fig, width="stretch",
                            config={"displayModeBar": False},
                        )

                # --- per-pair spread ---------------------------------
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown("**Spread z-score**")
                chosen = st.selectbox(
                    "Pair", [p.name for p in scan.pairs], label_visibility="collapsed"
                )
                pair = next(p for p in scan.pairs if p.name == chosen)

                if not pair.zscore.empty:
                    tail = pair.zscore.tail(500)
                    z_fig = go.Figure()
                    z_fig.add_trace(
                        go.Scatter(
                            x=tail.index, y=tail.values, mode="lines",
                            line={"color": "#7FC5E8", "width": 1.6},
                            name="z-score",
                            hovertemplate="%{x|%d %b %Y}<br>z = %{y:+.2f}<extra></extra>",
                        )
                    )
                    for level, colour, dash in [
                        (pair.entry_z, GOLD, "dash"),
                        (-pair.entry_z, GOLD, "dash"),
                        (pair.exit_z, GREEN, "dot"),
                        (-pair.exit_z, GREEN, "dot"),
                        (0, MUTED, "solid"),
                    ]:
                        z_fig.add_hline(
                            y=level, line_dash=dash, line_color=colour, line_width=1
                        )
                    z_fig.update_layout(**_chart_layout("z-score", height=320))
                    st.plotly_chart(
                        z_fig, width="stretch",
                        config={"displayModeBar": False},
                    )
                    st.markdown(
                        f'<div class="tiny">Gold = entry at ±{pair.entry_z:.2f} · '
                        f'green = exit at ±{pair.exit_z:.2f} · last 500 sessions · '
                        f'hedge ratio β = {pair.current_beta:.3f}</div>',
                        unsafe_allow_html=True,
                    )

                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown(
                    '<div class="tiny">⚠️ Backtests are not forecasts. Bands are fitted '
                    'in-sample and the gap between the in-sample and walk-forward Sharpe '
                    'is the honest measure of how much of the edge is real. Costs are '
                    'modelled at the level set above and exclude borrow, slippage and '
                    'financing. Shorting is required — check your employer\'s personal '
                    'account dealing rules before acting on any of this.</div>',
                    unsafe_allow_html=True,
                )


# ----------------------------------------------------------------- rules

with tab_rules:
    st.markdown("##### Rule compliance")
    st.markdown(
        '<div class="tiny">Computed in Python from the live book against '
        '<code>investor-os/investor-one-pager.md</code>. Arithmetic, not opinion.</div>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    if not checks["breaches"]:
        st.success("No breaches detected.")
    else:
        order = {"critical": 0, "warning": 1, "info": 2}
        for breach in sorted(
            checks["breaches"], key=lambda b: order.get(b["severity"], 9)
        ):
            tickers = breach.get("tickers") or []
            tail = ""
            if tickers and len(tickers) <= 8:
                tail = (
                    f'<div class="tiny" style="margin-top:0.45rem;">'
                    f'{", ".join(t for t in tickers if t)}</div>'
                )
            elif tickers:
                tail = (
                    f'<div class="tiny" style="margin-top:0.45rem;">'
                    f'{len(tickers)} positions</div>'
                )
            st.markdown(
                f'<div class="flag {breach["severity"]}">'
                f'<div class="rule">{breach["rule"]}</div>'
                f'<div class="detail">{breach["detail"]}</div>{tail}</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)
    a, b, c = st.columns(3)
    with a:
        st.markdown(
            card("Held in funds", f"{checks['fund_pct']:.1f}%",
                 f"{checks['fund_count']} funds · {money(checks['fund_value'])}",
                 "flat"),
            unsafe_allow_html=True,
        )
    with b:
        st.markdown(
            card("Attention per holding",
                 f"{checks['minutes_per_holding']:.1f} min",
                 f"{checks['position_count']} positions · 2 hrs/week", "down"),
            unsafe_allow_html=True,
        )
    with c:
        st.markdown(
            card("Deep losers", str(len(checks["deep_losers"])),
                 "positions at -20% or worse",
                 "down" if checks["deep_losers"] else "up"),
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------- market

with tab_market:
    st.markdown("##### Top 5 positions — live market data")

    top5 = top_positions(holdings, 5)
    quotes = _quotes(tuple(h["Ticker"] for h in top5))

    for holding in top5:
        raw = quotes.get(holding["Ticker"], {})
        quote = Quote(**raw) if raw else Quote(symbol="", ok=False, error="no data")
        weight = float(holding.get("Weight %") or 0) * 100
        book_pl = float(holding.get("Unrealised P/L %") or 0) * 100

        head, body = st.columns([2, 3])
        with head:
            st.markdown(f"**{holding['Name']}**")
            st.markdown(
                f'<div class="tiny">{t212_to_yahoo(holding["Ticker"])} · '
                f'{weight:.2f}% of book · held since {holding.get("Opened", "?")}</div>',
                unsafe_allow_html=True,
            )
            m1, m2 = st.columns(2)
            m1.metric("Value", money(holding.get("Current value")))
            m2.metric("Book P/L", f"{book_pl:+.1f}%")

        with body:
            if not quote.ok or quote.price is None:
                st.caption(f"Live data unavailable — {quote.error}")
            else:
                q1, q2, q3 = st.columns(3)
                q1.metric(
                    "Price",
                    f"{quote.price:,.2f} {quote.currency}",
                    f"{quote.change_pct:+.2f}%" if quote.change_pct is not None else None,
                )
                q2.metric(
                    "vs 52w high",
                    f"{quote.pct_off_52w_high:+.1f}%"
                    if quote.pct_off_52w_high is not None
                    else "—",
                )
                q3.metric("P/E", f"{quote.pe_ratio:.1f}" if quote.pe_ratio else "—")

                frame = _history(quote.symbol, "6mo")
                if frame is not None and not frame.empty and "Close" in frame:
                    st.line_chart(
                        frame["Close"],
                        height=130,
                        color=GREEN if book_pl >= 0 else RED,
                    )
        st.divider()

    st.markdown("##### Market backdrop")
    market = _market()
    columns = st.columns(4)
    for index, (label, raw) in enumerate(market.items()):
        quote = Quote(**raw)
        with columns[index % 4]:
            if quote.ok and quote.price is not None:
                st.metric(
                    label,
                    f"{quote.price:,.2f}",
                    f"{quote.change_pct:+.2f}%"
                    if quote.change_pct is not None
                    else None,
                )
            else:
                st.metric(label, "—")

    st.markdown("<br>", unsafe_allow_html=True)
    if configured and st.button("Run full market scan", width="content"):
        with st.spinner("Analysing the top 5…"):
            response = analyzer.market_scan()
        if response.ok:
            st.markdown(
                f'<div class="brief">{response.text}</div>', unsafe_allow_html=True
            )
        else:
            st.error(response.error)


# --------------------------------------------------------------- advisor

STARTERS = [
    "What is the single biggest problem with this book?",
    "Which positions am I holding out of inertia rather than conviction?",
    "How much do my funds and direct holdings overlap?",
]

if "chat" not in st.session_state:
    st.session_state.chat = memory.recent_messages(limit=20)


def ask(question: str) -> None:
    """Send a question and record both sides of the exchange."""
    st.session_state.chat.append({"role": "user", "content": question})
    with st.spinner("Thinking…"):
        response = analyzer.chat(question)
    st.session_state.chat.append(
        {
            "role": "assistant",
            "content": response.text if response.ok else f"⚠️ {response.error}",
        }
    )


with tab_advisor:
    st.markdown("##### Advisor")
    st.markdown(
        '<div class="tiny">Sees your one-pager, memory, live book and the rule '
        'engine on every message. Frames decisions — never makes them.</div>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    if not configured:
        st.info(
            "Add `GOOGLE_API_KEY` to `keys/.env` to enable the advisor. "
            "Free key at https://aistudio.google.com/apikey — the Overview, "
            "Holdings, Rules and Market tabs all work without it."
        )
    else:
        for message in st.session_state.chat:
            with st.chat_message(
                message["role"], avatar="🤖" if message["role"] == "assistant" else None
            ):
                st.markdown(message["content"])

        if question := st.chat_input("Ask about the book, a position, or a rule…"):
            ask(question)
            st.rerun()

        st.markdown("<br>", unsafe_allow_html=True)
        for column, starter in zip(st.columns(3), STARTERS):
            with column:
                if st.button(starter, width="stretch", key=f"tab_{starter[:18]}"):
                    ask(starter)
                    st.rerun()

        if st.button("Clear conversation"):
            memory.clear_messages()
            st.session_state.chat = []
            st.rerun()
