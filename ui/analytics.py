"""Risk metrics and dividend aggregation.

Two jobs, both feeding the Analytics and Dividends pages.

**Risk.** Trading 212 gives a single snapshot, not a value series, so the
portfolio's history is reconstructed: hold today's quantities constant
and revalue them at historical prices. That is an approximation and the
UI says so — it ignores interim buys and sells, and it ignores FX moves
because each holding's GBP value is scaled by its own local-currency
price path. It is the right shape for judging volatility and drawdown;
it is not a performance record.

**Dividends.** Straightforward aggregation of ``data/dividends.json``.

    from analytics import portfolio_series, risk_metrics
    series = portfolio_series(holdings, period="1y")
    metrics = risk_metrics(series)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from prices import get_history, t212_to_yahoo

ROOT = Path(__file__).resolve().parent.parent
DIVIDENDS_JSON = ROOT / "data" / "dividends.json"

TRADING_DAYS = 252
BENCHMARK = "^GSPC"          # S&P 500, for beta
DEFAULT_RISK_FREE = 0.04     # annual, used for Sharpe


# ------------------------------------------------------------------ types


@dataclass
class RiskMetrics:
    sharpe: float | None = None
    volatility: float | None = None       # annualised, decimal
    max_drawdown: float | None = None     # decimal, negative
    cagr: float | None = None             # decimal
    beta: float | None = None
    var95: float | None = None            # daily, decimal, negative
    var95_value: float | None = None      # daily, in currency
    observations: int = 0
    start_value: float | None = None
    end_value: float | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------- portfolio series


def portfolio_series(
    holdings: Iterable[dict], *, period: str = "1y", min_coverage: float = 0.55
) -> pd.Series:
    """Approximate portfolio value over time from today's holdings.

    Each holding contributes ``current_value × (price_t / price_latest)``,
    so the series ends at today's true book value and scales backwards on
    each instrument's own price path.

    Holdings list at different times — a recent IPO has no price before it
    listed. Rather than truncating the whole window to the newest holding
    (which would make every lookback identical), coverage is computed *per
    date*: the value of holdings that existed on that date is grossed up
    to full book value. Dates where coverage falls below ``min_coverage``
    are dropped, so the window self-trims to where the estimate is sound.

    The returned Series carries diagnostics in ``.attrs``: ``coverage``,
    ``priced``, ``missing``, ``requested_period``.
    """
    holdings = list(holdings)
    if not holdings:
        return pd.Series(dtype=float)

    total_value = sum(float(h.get("Current value") or 0.0) for h in holdings)
    if total_value <= 0:
        return pd.Series(dtype=float)

    contributions: list[pd.Series] = []
    weights: list[float] = []
    priced = 0
    missing: list[str] = []

    for holding in holdings:
        value = float(holding.get("Current value") or 0.0)
        if value <= 0:
            continue
        ticker = holding.get("Ticker", "")
        symbol = t212_to_yahoo(ticker)
        if not symbol:
            missing.append(ticker)
            continue

        history = get_history(symbol, period=period)
        if history is None or history.empty or "Close" not in history:
            missing.append(ticker)
            continue

        closes = history["Close"].dropna()
        if len(closes) < 5 or closes.iloc[-1] == 0:
            missing.append(ticker)
            continue

        contribution = (closes / closes.iloc[-1]) * value
        contribution.index = pd.to_datetime(contribution.index).tz_localize(None)
        contribution = contribution[~contribution.index.duplicated(keep="last")]

        contributions.append(contribution)
        weights.append(value)
        priced += 1

    if not contributions:
        return pd.Series(dtype=float)

    combined = pd.concat(contributions, axis=1).sort_index()
    # Forward-fill only within each column's own life, so a holding that
    # had not listed yet stays NaN rather than inheriting a phantom price.
    combined = combined.ffill()

    # Per-date covered book value: the weight of every column with data.
    present = combined.notna()
    covered_by_date = present.mul(weights, axis=1).sum(axis=1)

    usable = covered_by_date / total_value >= min_coverage
    combined, covered_by_date = combined[usable], covered_by_date[usable]
    if combined.empty:
        return pd.Series(dtype=float)

    # Sum what exists, then gross up by that date's coverage.
    series = combined.sum(axis=1, skipna=True) * (total_value / covered_by_date)
    series = series.dropna()

    series.attrs.update(
        {
            "coverage": float(covered_by_date.iloc[0] / total_value),
            "coverage_end": float(covered_by_date.iloc[-1] / total_value),
            "priced": priced,
            "missing": missing,
            "requested_period": period,
        }
    )
    return series


def drawdown_series(series: pd.Series) -> pd.Series:
    """Percentage below the running peak, as a negative decimal."""
    if series is None or series.empty:
        return pd.Series(dtype=float)
    return series / series.cummax() - 1.0


# --------------------------------------------------------------- metrics


def risk_metrics(
    series: pd.Series,
    *,
    risk_free: float = DEFAULT_RISK_FREE,
    benchmark: pd.Series | None = None,
    period: str = "1y",
) -> RiskMetrics:
    """Standard risk statistics from a value series."""
    if series is None or len(series) < 10:
        return RiskMetrics(note="Not enough price history to compute metrics.")

    returns = series.pct_change().dropna()
    returns = returns[np.isfinite(returns)]
    if returns.empty:
        return RiskMetrics(note="No usable returns in the series.")

    volatility = float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS))

    # Sharpe on excess returns, annualised.
    daily_rf = (1.0 + risk_free) ** (1.0 / TRADING_DAYS) - 1.0
    excess = returns - daily_rf
    sharpe = (
        float(excess.mean() / returns.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if returns.std(ddof=1) > 0
        else None
    )

    drawdowns = drawdown_series(series)
    max_drawdown = float(drawdowns.min()) if not drawdowns.empty else None

    start_value, end_value = float(series.iloc[0]), float(series.iloc[-1])
    years = max((series.index[-1] - series.index[0]).days / 365.25, 1e-9)
    cagr = (
        float((end_value / start_value) ** (1.0 / years) - 1.0)
        if start_value > 0 and years > 0.02
        else None
    )

    # Beta against the benchmark, aligned on shared dates.
    beta = None
    if benchmark is None:
        benchmark = _benchmark_series(period)
    if benchmark is not None and not benchmark.empty:
        bench_returns = benchmark.pct_change().dropna()
        aligned = pd.concat(
            [returns.rename("p"), bench_returns.rename("b")], axis=1
        ).dropna()
        if len(aligned) >= 20 and aligned["b"].var(ddof=1) > 0:
            beta = float(
                aligned["p"].cov(aligned["b"]) / aligned["b"].var(ddof=1)
            )

    # Historical VaR — the 5th percentile of daily returns.
    var95 = float(np.percentile(returns, 5))

    return RiskMetrics(
        sharpe=sharpe,
        volatility=volatility,
        max_drawdown=max_drawdown,
        cagr=cagr,
        beta=beta,
        var95=var95,
        var95_value=var95 * end_value,
        observations=len(returns),
        start_value=start_value,
        end_value=end_value,
    )


def _benchmark_series(period: str) -> pd.Series | None:
    history = get_history(BENCHMARK, period=period)
    if history is None or history.empty or "Close" not in history:
        return None
    closes = history["Close"].dropna()
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    return closes


# ------------------------------------------------------------- dividends


def load_dividends(path: Path | str = DIVIDENDS_JSON) -> pd.DataFrame:
    """Read data/dividends.json into a tidy frame. Empty if absent."""
    path = Path(path)
    columns = ["Name", "Ticker", "Paid on", "Amount", "Quantity",
               "Per share", "Currency"]
    if not path.exists():
        return pd.DataFrame(columns=columns)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return pd.DataFrame(columns=columns)

    frame = pd.DataFrame(payload.get("dividends") or [])
    if frame.empty:
        return pd.DataFrame(columns=columns)

    frame["Paid on"] = pd.to_datetime(frame["Paid on"], errors="coerce")
    frame = frame.dropna(subset=["Paid on"])
    frame["Amount"] = pd.to_numeric(frame["Amount"], errors="coerce").fillna(0.0)

    if "Name" not in frame:
        frame["Name"] = frame.get("Ticker", "")
    if "Currency" not in frame:
        frame["Currency"] = payload.get("currency", "GBP")

    return frame.sort_values("Paid on", ascending=False).reset_index(drop=True)


def dividend_summary(frame: pd.DataFrame) -> dict:
    """Headline figures for the Dividends page."""
    if frame is None or frame.empty:
        return {
            "total": 0.0, "ytd": 0.0, "avg_monthly": 0.0,
            "payments": 0, "payers": 0, "first": None, "last": None,
            "ttm": 0.0,
        }

    total = float(frame["Amount"].sum())
    this_year = frame[frame["Paid on"].dt.year == date.today().year]
    ytd = float(this_year["Amount"].sum())

    first, last = frame["Paid on"].min(), frame["Paid on"].max()
    months = max((last.year - first.year) * 12 + (last.month - first.month) + 1, 1)

    cutoff = pd.Timestamp(date.today()) - pd.DateOffset(years=1)
    ttm = float(frame[frame["Paid on"] >= cutoff]["Amount"].sum())

    return {
        "total": total,
        "ytd": ytd,
        "avg_monthly": total / months,
        "payments": int(len(frame)),
        "payers": int(frame["Ticker"].nunique()),
        "first": first,
        "last": last,
        "ttm": ttm,
    }


def monthly_dividends(frame: pd.DataFrame) -> pd.DataFrame:
    """Total received per calendar month, with empty months filled in."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Month", "Amount"])

    grouped = (
        frame.set_index("Paid on")["Amount"].resample("MS").sum().reset_index()
    )
    grouped.columns = ["Month", "Amount"]
    return grouped


def dividends_by_holding(frame: pd.DataFrame, limit: int = 15) -> pd.DataFrame:
    """Top payers by total received."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Name", "Ticker", "Amount", "Payments"])

    grouped = (
        frame.groupby(["Name", "Ticker"], as_index=False)
        .agg(Amount=("Amount", "sum"), Payments=("Amount", "size"))
        .sort_values("Amount", ascending=False)
        .head(limit)
        .reset_index(drop=True)
    )
    return grouped


def yield_on_book(frame: pd.DataFrame, book_value: float) -> float | None:
    """Trailing-twelve-month dividends as a percentage of book value."""
    if frame is None or frame.empty or not book_value:
        return None
    return dividend_summary(frame)["ttm"] / book_value * 100.0


if __name__ == "__main__":
    from ui.prices import load_portfolio

    book = load_portfolio()
    series = portfolio_series(book.get("holdings", []), period="6mo")
    print(f"Series points: {len(series)}")
    if not series.empty:
        print(risk_metrics(series).as_dict())

    dividends = load_dividends()
    print(f"\nDividend rows: {len(dividends)}")
    print(dividend_summary(dividends))
