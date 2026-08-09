#!/usr/bin/env python3
"""
pairs_walkforward.py
====================

Self-contained statistical-arbitrage pipeline, distilled from notebooks 1-6 of the
Kalman pairs-trading project. No pre-built dataset required: prices are pulled from
Yahoo Finance for either an index universe or a hand-picked list of tickers.

Pipeline
--------
1. Download + clean prices (coverage filter, forward-fill, log prices).
2. Screen every within-universe pair on the *screening sample* only:
     - I(1) check (ADF on levels and on first differences, both legs)
     - Engle-Granger cointegration test (p < --coint-alpha)
     - rolling-window cointegration stability (pass rate + minimum #windows)
3. Diversify the book (spread-return correlation filter, optional unique legs).
4. Walk-forward rolling-window backtest ONLY:
     for each fold -> re-anchor the Kalman filter on the trailing context window,
     trade the next `--oos-step` days, step forward, concatenate the traded chunks.
   Hedge ratios come from a causal Kalman filter; z-scores from a trailing window;
   positions are lagged one day; costs are charged on both legs' turnover.

Everything else in the original notebooks (static full-sample backtest, refined
signal, dynamic pair re-selection) is deliberately omitted.

Usage
-----
    # whole index
    python pairs_walkforward.py --index hsi --start 2015-01-01

    # your own list
    python pairs_walkforward.py --tickers 0700.HK 9988.HK 1810.HK 3690.HK

    # a single pair you already have in mind
    python pairs_walkforward.py --tickers 0857.HK 0386.HK

    # no network / check the install works
    python pairs_walkforward.py --selftest

Requires: pandas, numpy, statsmodels, matplotlib, yfinance (+ lxml for index scraping)
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

TRADING_DAYS = 252


# ----------------------------------------------------------------------------------
# 1. Universe / data
# ----------------------------------------------------------------------------------

INDEX_SOURCES = {
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", ""),
    "nasdaq100": ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", ""),
    "ftse100": ("https://en.wikipedia.org/wiki/FTSE_100_Index", "Ticker", ".L"),
    "dax": ("https://en.wikipedia.org/wiki/DAX", "Ticker", ""),
    "hsi": ("https://en.wikipedia.org/wiki/Hang_Seng_Index", "Ticker", ""),
    "sti": ("https://en.wikipedia.org/wiki/Straits_Times_Index", "Ticker", ".SI"),
    "nikkei225": ("https://en.wikipedia.org/wiki/Nikkei_225", "Ticker", ".T"),
}


def _clean_hk_ticker(raw: str) -> str:
    """'SEHK: 700' / '700' / '0700.HK' -> '0700.HK'."""
    s = str(raw).replace("SEHK:", "").replace("SEHK", "").strip()
    if s.upper().endswith(".HK"):
        return s.upper().zfill(7)
    digits = "".join(ch for ch in s if ch.isdigit())
    return f"{int(digits):04d}.HK" if digits else s


def index_constituents(index_name: str, max_tickers: Optional[int] = None) -> List[str]:
    """Scrape index members from Wikipedia. Requires lxml."""
    key = index_name.lower()
    if key not in INDEX_SOURCES:
        raise ValueError(
            f"Unknown index '{index_name}'. Known: {', '.join(sorted(INDEX_SOURCES))}. "
            "Use --tickers or --tickers-file for anything else."
        )
    url, col_hint, suffix = INDEX_SOURCES[key]
    try:
        tables = pd.read_html(url)
    except ImportError as e:
        raise SystemExit("Index scraping needs lxml: pip install lxml") from e
    except Exception as e:
        raise SystemExit(
            f"Could not read the constituents table for {index_name} ({e}). "
            "Pass the tickers directly with --tickers or --tickers-file instead."
        ) from e

    tickers: List[str] = []
    for tbl in tables:
        cols = [str(c) for c in tbl.columns]
        match = [c for c in cols if col_hint.lower() in c.lower()]
        if not match:
            continue
        vals = tbl[match[0]].dropna().astype(str).tolist()
        if len(vals) >= 10:  # a constituents table, not a sidebar
            tickers = vals
            break
    if not tickers:
        raise RuntimeError(f"Could not locate a constituents table at {url}")

    if key == "hsi":
        out = [_clean_hk_ticker(t) for t in tickers]
    else:
        out = [t.strip().replace(".", "-") + suffix if suffix else t.strip().replace(".", "-")
               for t in tickers]
    out = [t for t in dict.fromkeys(out) if t and t.lower() != "nan"]
    return out[:max_tickers] if max_tickers else out


def download_prices(tickers: Sequence[str], start: str, end: Optional[str]) -> pd.DataFrame:
    """Adjusted close prices, one column per ticker."""
    import yfinance as yf

    print(f"Downloading {len(tickers)} tickers from Yahoo Finance ...")
    raw = yf.download(
        list(tickers), start=start, end=end, auto_adjust=True,
        progress=False, group_by="column", threads=True,
    )
    if raw is None or len(raw) == 0:
        raise RuntimeError("Yahoo Finance returned no data — check the tickers and dates.")
    px = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(px, pd.Series):
        px = px.to_frame(tickers[0])
    px.columns = [str(c) for c in px.columns]
    return px.sort_index()


def clean_prices(px: pd.DataFrame, min_coverage: float = 0.5) -> pd.DataFrame:
    """Drop thin names, forward-fill internal gaps, return LOG prices.

    Leading pre-listing NaNs are intentionally left as NaN — no price existed.
    Each pair is later tested on its own overlapping window.
    """
    px = px.dropna(axis=1, how="all")
    keep = px.notna().mean() >= min_coverage
    dropped = sorted(px.columns[~keep])
    px = px.loc[:, keep].ffill()
    px = px.loc[:, (px > 0).all(skipna=True)]
    if dropped:
        print(f"  dropped {len(dropped)} tickers below {min_coverage:.0%} coverage: "
              f"{', '.join(dropped[:12])}{' ...' if len(dropped) > 12 else ''}")
    print(f"  universe: {px.shape[1]} tickers | {px.index.min().date()} -> {px.index.max().date()}")
    return np.log(px)


# ----------------------------------------------------------------------------------
# 2. Cointegration screen (Engle-Granger + rolling stability)
# ----------------------------------------------------------------------------------

def _adf_pvalue(series: pd.Series) -> float:
    from statsmodels.tsa.stattools import adfuller
    s = pd.Series(series).dropna()
    if len(s) < 30 or s.std() == 0:
        return np.nan
    try:
        return float(adfuller(s, autolag="AIC")[1])
    except Exception:
        return np.nan


def is_i1(series: pd.Series, alpha: float = 0.05) -> bool:
    """Non-stationary in levels, stationary in first differences."""
    p_level = _adf_pvalue(series)
    p_diff = _adf_pvalue(series.diff())
    if np.isnan(p_level) or np.isnan(p_diff):
        return False
    return (p_level > alpha) and (p_diff < alpha)


def ols_hedge_ratio(y: pd.Series, x: pd.Series) -> Tuple[float, float]:
    """y = alpha + beta * x, returned as (alpha, beta)."""
    import statsmodels.api as sm
    df = pd.concat([y, x], axis=1).dropna()
    if len(df) < 30:
        return np.nan, np.nan
    res = sm.OLS(df.iloc[:, 0].values, sm.add_constant(df.iloc[:, 1].values)).fit()
    return float(res.params[0]), float(res.params[1])


def engle_granger(y: pd.Series, x: pd.Series) -> float:
    """Engle-Granger two-step cointegration p-value."""
    from statsmodels.tsa.stattools import coint
    df = pd.concat([y, x], axis=1).dropna()
    if len(df) < 60:
        return np.nan
    try:
        return float(coint(df.iloc[:, 0], df.iloc[:, 1])[1])
    except Exception:
        return np.nan


def rolling_coint_stability(
    y: pd.Series, x: pd.Series, window: int = 504, step: int = 21, alpha: float = 0.05
) -> Tuple[float, int]:
    """Fraction of rolling windows in which the pair cointegrates, and #windows.

    A pass rate is only as trustworthy as the number of windows behind it — with a
    large window and a short sample the 'stability' check degenerates back into the
    full-sample test it is meant to guard against, hence n_windows is returned too.
    """
    df = pd.concat([y, x], axis=1).dropna()
    if len(df) < window:
        return np.nan, 0
    passes, n = 0, 0
    for start in range(0, len(df) - window + 1, step):
        sub = df.iloc[start:start + window]
        p = engle_granger(sub.iloc[:, 0], sub.iloc[:, 1])
        if not np.isnan(p):
            n += 1
            passes += int(p < alpha)
    return (passes / n if n else np.nan), n


@dataclass
class PairSpec:
    y: str
    x: str
    beta: float = np.nan
    alpha_ols: float = np.nan
    coint_p: float = np.nan
    rolling_pass_rate: float = np.nan
    n_windows: int = 0
    weight: float = 0.0

    @property
    def name(self) -> str:
        return f"{self.y} vs {self.x}"


def screen_pairs(
    log_px: pd.DataFrame,
    coint_alpha: float = 0.05,
    adf_alpha: float = 0.05,
    rolling_window: int = 504,
    rolling_step: int = 21,
    min_pass_rate: float = 0.5,
    min_windows: int = 10,
    max_pairs: Optional[int] = None,
    skip_stability: bool = False,
    skip_i1: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """Full screen on the screening sample. Returns one row per candidate pair."""
    cols = list(log_px.columns)
    combos = list(itertools.combinations(cols, 2))
    if max_pairs and len(combos) > max_pairs:
        print(f"  {len(combos)} candidate pairs -> truncating to {max_pairs} "
              "(raise with --max-pairs)")
        combos = combos[:max_pairs]

    # Cache the I(1) verdict per ticker so it is computed once, not once per pair.
    i1_cache = {c: (True if skip_i1 else is_i1(log_px[c], adf_alpha)) for c in cols}
    n_i1 = sum(i1_cache.values())
    if verbose:
        print(f"  I(1) legs: {n_i1}/{len(cols)} | testing {len(combos)} pairs ...")

    rows = []
    for i, (a, b) in enumerate(combos, 1):
        if verbose and i % 250 == 0:
            print(f"    ... {i}/{len(combos)}")
        if not (i1_cache.get(a) and i1_cache.get(b)):
            continue
        p = engle_granger(log_px[a], log_px[b])
        if np.isnan(p) or p >= coint_alpha:
            continue
        alpha_ols, beta = ols_hedge_ratio(log_px[a], log_px[b])
        if np.isnan(beta) or beta <= 0:
            continue  # negative hedge ratio => not an economically sensible spread
        if skip_stability:
            pass_rate, n_win = np.nan, 0
        else:
            pass_rate, n_win = rolling_coint_stability(
                log_px[a], log_px[b], rolling_window, rolling_step, coint_alpha
            )
        rows.append({
            "y": a, "x": b, "pair": f"{a} vs {b}", "coint_p": p,
            "beta": beta, "alpha_ols": alpha_ols,
            "rolling_pass_rate": pass_rate, "n_windows": n_win,
        })

    res = pd.DataFrame(rows)
    if res.empty:
        return res
    if not skip_stability:
        res["stable"] = (res["rolling_pass_rate"] >= min_pass_rate) & (res["n_windows"] >= min_windows)
    else:
        res["stable"] = True
    return res.sort_values(["stable", "coint_p"], ascending=[False, True]).reset_index(drop=True)


def diversify(
    log_px: pd.DataFrame, candidates: pd.DataFrame,
    max_corr: float = 0.7, unique_legs: bool = True, max_book: int = 10,
) -> List[PairSpec]:
    """Greedy selection by p-value: drop pairs whose spread returns duplicate an
    already-accepted pair, and (optionally) forbid a stock anchoring two pairs."""
    accepted: List[PairSpec] = []
    spread_rets: Dict[str, pd.Series] = {}
    used_legs: set = set()

    for _, r in candidates[candidates["stable"]].iterrows():
        if len(accepted) >= max_book:
            break
        if unique_legs and ({r["y"], r["x"]} & used_legs):
            continue
        spread = log_px[r["y"]] - r["beta"] * log_px[r["x"]]
        sr = spread.diff().dropna()
        if any(abs(sr.corr(prev)) > max_corr for prev in spread_rets.values()):
            continue
        spec = PairSpec(y=r["y"], x=r["x"], beta=r["beta"], alpha_ols=r["alpha_ols"],
                        coint_p=r["coint_p"], rolling_pass_rate=r["rolling_pass_rate"],
                        n_windows=int(r["n_windows"]))
        accepted.append(spec)
        spread_rets[spec.name] = sr
        used_legs |= {r["y"], r["x"]}

    w = 1.0 / len(accepted) if accepted else 0.0
    for s in accepted:
        s.weight = w
    return accepted


# ----------------------------------------------------------------------------------
# 3. Kalman filter — dynamic hedge ratio
# ----------------------------------------------------------------------------------

def kalman_hedge_ratio(
    y: pd.Series, x: pd.Series, obs_cov: float = 1.0, delta: float = 1e-4,
    init_alpha: float = 0.0, init_beta: float = 1.0,
) -> pd.DataFrame:
    """Time-varying [alpha_t, beta_t] via a random-walk state-space model.

        y_t = alpha_t + beta_t * x_t + e_t,   e_t ~ N(0, R)
        [alpha_t, beta_t] = [alpha_{t-1}, beta_{t-1}] + w_t,  w_t ~ N(0, Q)

    Q = delta/(1-delta) * I. Purely recursive, so every value at t uses only
    information up to and including t — no look-ahead.

    obs_cov (R) trades responsiveness against robustness: low R chases noise,
    high R lags structural breaks. R = 1.0 is the project default.
    """
    df = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
    if df.empty:
        return pd.DataFrame(columns=["y", "x", "alpha_t", "beta_t", "spread"])

    Q = delta / (1.0 - delta) * np.eye(2)
    R = float(obs_cov)
    theta = np.array([init_alpha, init_beta], dtype=float)
    P = np.eye(2)

    alphas = np.empty(len(df))
    betas = np.empty(len(df))
    yv, xv = df["y"].to_numpy(), df["x"].to_numpy()

    for t in range(len(df)):
        H = np.array([1.0, xv[t]])
        P_pred = P + Q
        S = H @ P_pred @ H.T + R
        K = (P_pred @ H) / S
        resid = yv[t] - H @ theta
        theta = theta + K * resid
        P = P_pred - np.outer(K, H) @ P_pred
        alphas[t], betas[t] = theta[0], theta[1]

    out = df.copy()
    out["alpha_t"] = alphas
    out["beta_t"] = betas
    out["spread"] = out["y"] - out["alpha_t"] - out["beta_t"] * out["x"]
    return out


# ----------------------------------------------------------------------------------
# 4. Signal + per-pair PnL
# ----------------------------------------------------------------------------------

def zscore_signal(
    details: pd.DataFrame, entry_z: float = 1.5, exit_z: float = 0.5, z_window: int = 60
) -> pd.DataFrame:
    """Trailing-window z-score of the Kalman spread -> {-1, 0, +1} position.

        z >  entry -> short the spread (-1)
        z < -entry -> long  the spread (+1)
        |z| <= exit -> flat
        otherwise   -> hold the previous position
    """
    d = details.copy()
    mu = d["spread"].rolling(z_window, min_periods=z_window).mean()
    sd = d["spread"].rolling(z_window, min_periods=z_window).std()
    d["zscore"] = (d["spread"] - mu) / sd
    d = d.dropna(subset=["zscore"])
    if d.empty:
        d["position"] = pd.Series(dtype=float)
        return d

    z = d["zscore"].to_numpy()
    pos = np.zeros(len(z))
    cur = 0.0
    for t in range(len(z)):
        if z[t] > entry_z:
            cur = -1.0
        elif z[t] < -entry_z:
            cur = 1.0
        elif abs(z[t]) <= exit_z:
            cur = 0.0
        pos[t] = cur
    d["position"] = pos
    return d


def pair_returns(signals: pd.DataFrame, cost_bps: float = 15.0) -> pd.DataFrame:
    """Daily net return per unit of gross capital deployed in the pair.

    Legs use log-price differences. The spread return is normalised by the gross
    exposure (1 + |beta|) so pairs with different hedge ratios are comparable and
    the portfolio weights mean what they say. Positions and betas are lagged one
    day: the signal formed on the close of t-1 is what earns the return on t.
    """
    d = signals.copy()
    r_y = d["y"].diff()
    r_x = d["x"].diff()
    beta_lag = d["beta_t"].shift(1)
    pos_lag = d["position"].shift(1)
    gross = 1.0 + beta_lag.abs()

    d["spread_ret"] = (r_y - beta_lag * r_x) / gross
    d["gross_ret"] = pos_lag * d["spread_ret"]

    # Turnover on both legs, in units of gross capital.
    leg_y = (d["position"] - d["position"].shift(1)).abs()
    leg_x = (d["position"] * d["beta_t"] - (d["position"] * d["beta_t"]).shift(1)).abs()
    d["cost"] = (cost_bps / 1e4) * (leg_y + leg_x) / gross
    d["net_ret"] = (d["gross_ret"].fillna(0.0) - d["cost"].fillna(0.0))
    return d


# ----------------------------------------------------------------------------------
# 5. Walk-forward rolling-window backtest  (the only backtest in this script)
# ----------------------------------------------------------------------------------

def walk_forward_backtest(
    log_px: pd.DataFrame,
    pairs: List[PairSpec],
    is_window: int = 262,
    oos_step: int = 63,
    entry_z: float = 1.5,
    exit_z: float = 0.5,
    z_window: int = 60,
    obs_cov: float = 1.0,
    delta: float = 1e-4,
    cost_bps: float = 15.0,
    start_from: Optional[pd.Timestamp] = None,
) -> Tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    """Rolling-origin evaluation.

    For every fold: re-anchor the Kalman filter on the trailing `is_window` days
    (OLS on that context sets the prior), run the filter and the z-score across
    context + trading window, then keep ONLY the `oos_step` trading days. Costs
    are charged over the full context and sliced with everything else, so the
    position carried into the fold is priced correctly.

    Nothing at date t uses data after t: the filter is recursive, the z-score
    window is trailing, and positions are lagged.

    Returns (portfolio daily returns, per-pair daily returns, fold log).
    """
    idx = log_px.index
    if start_from is not None:
        first = int(idx.searchsorted(pd.Timestamp(start_from)))
    else:
        first = 0
    origin = max(first, is_window)
    if origin >= len(idx):
        raise ValueError("Not enough history for even one walk-forward fold.")

    fold_returns: List[pd.DataFrame] = []
    fold_rows: List[dict] = []

    for fold, s in enumerate(range(origin, len(idx), oos_step), start=1):
        ctx = idx[max(0, s - is_window):s]
        trade = idx[s:s + oos_step]
        if len(trade) < 5 or len(ctx) < max(z_window + 5, 60):
            continue

        chunk: Dict[str, pd.Series] = {}
        for p in pairs:
            span = ctx.union(trade)
            y = log_px[p.y].reindex(span)
            x = log_px[p.x].reindex(span)
            ctx_ok = pd.concat([y.loc[ctx], x.loc[ctx]], axis=1).dropna()
            if len(ctx_ok) < max(z_window + 5, 60):
                continue

            a0, b0 = ols_hedge_ratio(y.loc[ctx], x.loc[ctx])
            if np.isnan(b0):
                continue

            details = kalman_hedge_ratio(y, x, obs_cov=obs_cov, delta=delta,
                                         init_alpha=a0, init_beta=b0)
            sig = zscore_signal(details, entry_z, exit_z, z_window)
            if sig.empty:
                continue
            rets = pair_returns(sig, cost_bps)
            out = rets.loc[rets.index.isin(trade)]
            if out.empty:
                continue

            chunk[p.name] = out["net_ret"] * p.weight
            fold_rows.append({
                "fold": fold,
                "ctx_start": ctx[0].date(), "ctx_end": ctx[-1].date(),
                "trade_start": out.index[0].date(), "trade_end": out.index[-1].date(),
                "pair": p.name, "weight": p.weight,
                "beta_start": float(out["beta_t"].iloc[0]),
                "beta_end": float(out["beta_t"].iloc[-1]),
                "n_trades": float(out["position"].diff().abs().sum() / 2),
                "net_ret": float(out["net_ret"].sum()),
            })

        if chunk:
            fold_returns.append(pd.DataFrame(chunk))

    if not fold_returns:
        raise RuntimeError("No fold produced any tradeable data.")

    per_pair = pd.concat(fold_returns).sort_index()
    per_pair = per_pair[~per_pair.index.duplicated(keep="first")]
    portfolio = per_pair.sum(axis=1).rename("portfolio")
    return portfolio, per_pair, pd.DataFrame(fold_rows)


# ----------------------------------------------------------------------------------
# 6. Performance metrics
# ----------------------------------------------------------------------------------

def performance_metrics(ret: pd.Series, rf_annual: float = 0.0, bootstrap: int = 0) -> dict:
    r = pd.Series(ret).dropna()
    if r.empty:
        return {}
    rf_daily = rf_annual / TRADING_DAYS
    excess = r - rf_daily
    ann_ret = (1 + r).prod() ** (TRADING_DAYS / len(r)) - 1
    ann_vol = r.std() * np.sqrt(TRADING_DAYS)
    sharpe = excess.mean() / r.std() * np.sqrt(TRADING_DAYS) if r.std() > 0 else np.nan
    downside = r[r < 0].std()
    sortino = excess.mean() / downside * np.sqrt(TRADING_DAYS) if downside and downside > 0 else np.nan
    cum = (1 + r).cumprod()
    dd = (cum / cum.cummax() - 1.0)
    max_dd = dd.min()

    m = {
        "start": r.index[0].date(), "end": r.index[-1].date(), "days": len(r),
        "ann_return": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe, "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": ann_ret / abs(max_dd) if max_dd < 0 else np.nan,
        "exposure": (r != 0).mean(),
        "hit_rate": (r[r != 0] > 0).mean() if (r != 0).any() else np.nan,
        "total_return": cum.iloc[-1] - 1,
    }
    if bootstrap:
        rng = np.random.default_rng(42)
        arr = excess.to_numpy()
        sims = []
        for _ in range(bootstrap):
            s = rng.choice(arr, size=len(arr), replace=True)
            if s.std() > 0:
                sims.append(s.mean() / s.std() * np.sqrt(TRADING_DAYS))
        if sims:
            m["sharpe_ci_lo"], m["sharpe_ci_hi"] = np.percentile(sims, [2.5, 97.5])
    return m


def rolling_sharpe(ret: pd.Series, window: int = TRADING_DAYS) -> pd.Series:
    mu = ret.rolling(window).mean()
    sd = ret.rolling(window).std()
    return (mu / sd) * np.sqrt(TRADING_DAYS)


# ----------------------------------------------------------------------------------
# 7. Plots
# ----------------------------------------------------------------------------------

def make_plots(ret: pd.Series, outdir: str, split: Optional[pd.Timestamp], label: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    cum = 100 * (1 + ret.fillna(0)).cumprod()
    dd = (cum / cum.cummax() - 1) * 100
    rs = rolling_sharpe(ret)

    fig, axes = plt.subplots(3, 1, figsize=(13, 11), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1, 1]})
    axes[0].plot(cum.index, cum, color="darkorange", lw=1.4, label="Walk-forward")
    axes[0].axhline(100, color="grey", ls=":", lw=0.8)
    if split is not None:
        axes[0].axvline(split, color="black", ls="--", lw=1.1, label="end of screening sample")
    axes[0].set_yscale("log")
    axes[0].set_ylabel("Portfolio value (base = 100, log)")
    axes[0].set_title(f"Walk-forward cumulative PnL | {label}")
    axes[0].legend(); axes[0].grid(alpha=0.3)

    axes[1].fill_between(dd.index, dd, 0, color="firebrick", alpha=0.5)
    axes[1].set_ylabel("Drawdown (%)"); axes[1].grid(alpha=0.3)

    axes[2].plot(rs.index, rs, color="steelblue", lw=1.2)
    axes[2].axhline(0, color="grey", ls=":", lw=0.8)
    axes[2].set_ylabel("Rolling 12m Sharpe"); axes[2].grid(alpha=0.3)
    axes[2].xaxis.set_major_locator(mdates.YearLocator())
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    plt.tight_layout()
    path = os.path.join(outdir, "walkforward_performance.png")
    plt.savefig(path, dpi=200, bbox_inches="tight")
    plt.close()
    print(f"  plot -> {path}")


# ----------------------------------------------------------------------------------
# 8. Orchestration
# ----------------------------------------------------------------------------------

def run(args, log_px: Optional[pd.DataFrame] = None) -> dict:
    if log_px is None:
        if args.tickers_file:
            with open(args.tickers_file) as f:
                tickers = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        elif args.tickers:
            tickers = list(args.tickers)
        elif args.index:
            tickers = index_constituents(args.index, args.max_tickers)
            print(f"{args.index.upper()}: {len(tickers)} constituents")
        else:
            raise SystemExit("Give me a universe: --index, --tickers or --tickers-file.")
        if args.max_tickers:
            tickers = tickers[:args.max_tickers]
        px = download_prices(tickers, args.start, args.end)
        log_px = clean_prices(px, args.min_coverage)

    if log_px.shape[1] < 2:
        raise SystemExit("Need at least 2 usable tickers after cleaning.")

    os.makedirs(args.outdir, exist_ok=True)

    # --- screening sample: only the first `screen_frac` of history is used to pick pairs
    n = len(log_px)
    split_i = int(n * args.screen_frac)
    screen_px = log_px.iloc[:split_i]
    split_date = log_px.index[split_i - 1] if 0 < split_i < n else None
    print(f"\nScreening sample: {screen_px.index.min().date()} -> {screen_px.index.max().date()} "
          f"({len(screen_px)} days, {args.screen_frac:.0%} of history)")

    rolling_window = args.rolling_window
    if rolling_window >= len(screen_px):
        rolling_window = max(126, len(screen_px) // 3)
        print(f"  screening sample too short for a {args.rolling_window}d stability window "
              f"-> using {rolling_window}d")

    two_ticker_mode = log_px.shape[1] == 2
    forced = False
    candidates = screen_pairs(
        screen_px,
        coint_alpha=args.coint_alpha, adf_alpha=args.adf_alpha,
        rolling_window=rolling_window, rolling_step=args.rolling_step,
        min_pass_rate=args.min_pass_rate, min_windows=args.min_windows,
        max_pairs=args.max_pairs, skip_stability=args.no_stability,
        skip_i1=args.no_i1,
    )

    if candidates.empty:
        if two_ticker_mode:
            a, b = list(log_px.columns)
            print(f"\n{a} vs {b} does NOT pass the cointegration screen "
                  f"(EG p = {engle_granger(screen_px[a], screen_px[b]):.3f}).")
            if not args.force:
                print("Nothing to trade. Re-run with --force to backtest it anyway.")
                return {}
            alpha_ols, beta = ols_hedge_ratio(screen_px[a], screen_px[b])
            candidates = pd.DataFrame([{
                "y": a, "x": b, "pair": f"{a} vs {b}",
                "coint_p": engle_granger(screen_px[a], screen_px[b]),
                "beta": beta, "alpha_ols": alpha_ols,
                "rolling_pass_rate": np.nan, "n_windows": 0, "stable": True,
            }])
            forced = True
            print("--force set: trading it regardless — treat the results as illustrative, "
                  "not as evidence of a tradeable relationship.")
            if candidates["beta"].iloc[0] <= 0:
                print("  note: the OLS hedge ratio is negative, so the 'spread' is really a "
                      "long-long combination rather than a hedged pair.")
        else:
            print("\nNo pair cleared the screen. Loosen --coint-alpha / --min-pass-rate, "
                  "widen the universe, or lengthen the sample.")
            return {}

    candidates.to_csv(os.path.join(args.outdir, "candidate_pairs.csv"), index=False)
    stable = candidates[candidates["stable"]]
    if forced:
        print("\nScreen not passed — proceeding under --force with 1 pair.")
    else:
        print(f"\nCointegrated (p < {args.coint_alpha}): {len(candidates)} | "
              f"survives rolling stability: {len(stable)}")

    if stable.empty and two_ticker_mode and args.force:
        candidates["stable"] = True
        stable = candidates
    if stable.empty:
        print("Nothing survived the stability filter — try --no-stability or a lower "
              "--min-pass-rate.")
        return {}

    book = diversify(log_px, candidates, max_corr=args.max_corr,
                     unique_legs=not args.allow_shared_legs, max_book=args.max_book)
    print(f"\nBook ({len(book)} pairs, equal-weighted at {book[0].weight:.1%} each):")
    for p in book:
        pr = "n/a" if np.isnan(p.rolling_pass_rate) else f"{p.rolling_pass_rate:.0%}"
        print(f"  {p.name:38}  beta={p.beta:6.3f}  p={p.coint_p:.4f}  "
              f"stability={pr} (n={p.n_windows})")

    # --- walk-forward
    print(f"\nWalk-forward: {args.is_window}d context / {args.oos_step}d trading, "
          f"entry z={args.entry_z}, exit z={args.exit_z}, R={args.obs_cov}, "
          f"costs={args.cost_bps}bps per leg")
    portfolio, per_pair, folds = walk_forward_backtest(
        log_px, book,
        is_window=args.is_window, oos_step=args.oos_step,
        entry_z=args.entry_z, exit_z=args.exit_z, z_window=args.z_window,
        obs_cov=args.obs_cov, delta=args.delta, cost_bps=args.cost_bps,
    )
    print(f"  {folds['fold'].nunique()} folds | {len(portfolio)} trading days | "
          f"{portfolio.index.min().date()} -> {portfolio.index.max().date()}")

    rows = [{"scope": "Full walk-forward",
             **performance_metrics(portfolio, args.rf, args.bootstrap)}]
    if split_date is not None:
        post = portfolio.loc[portfolio.index > split_date]
        if len(post) > TRADING_DAYS // 4:
            rows.append({"scope": "After screening sample (clean OOS)",
                         **performance_metrics(post, args.rf, args.bootstrap)})
    metrics = pd.DataFrame(rows).set_index("scope")

    pd.set_option("display.width", 200, "display.max_columns", 50)
    print("\nPerformance\n" + "-" * 60)
    print(metrics.T.to_string(float_format=lambda v: f"{v:,.4f}"))

    portfolio.to_csv(os.path.join(args.outdir, "portfolio_returns.csv"))
    per_pair.to_csv(os.path.join(args.outdir, "pair_returns.csv"))
    folds.to_csv(os.path.join(args.outdir, "fold_log.csv"), index=False)
    metrics.to_csv(os.path.join(args.outdir, "metrics.csv"))
    print(f"\nSaved CSVs -> {os.path.abspath(args.outdir)}")

    if not args.no_plots:
        make_plots(portfolio, args.outdir, split_date,
                   f"{len(book)} pairs | entry {args.entry_z} / exit {args.exit_z}")

    return {"portfolio": portfolio, "per_pair": per_pair, "folds": folds,
            "metrics": metrics, "book": book, "candidates": candidates}


# ----------------------------------------------------------------------------------
# 9. Self-test (no network)
# ----------------------------------------------------------------------------------

def synthetic_panel(n: int = 2600, seed: int = 7) -> pd.DataFrame:
    """Two genuinely cointegrated legs, one extra cointegrated pair, two random walks."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    common = np.cumsum(rng.normal(0.0004, 0.012, n))
    common2 = np.cumsum(rng.normal(0.0003, 0.010, n))

    def ou(kappa=0.08, sigma=0.02):
        e = np.zeros(n)
        for t in range(1, n):
            e[t] = e[t - 1] * (1 - kappa) + rng.normal(0, sigma)
        return e

    data = {
        "COINT_A": 3.0 + common,
        "COINT_B": 2.4 + 0.9 * common + ou(),
        "COINT_C": 3.5 + common2,
        "COINT_D": 3.1 + 1.1 * common2 + ou(),
        "NOISE_E": 2.0 + np.cumsum(rng.normal(0.0002, 0.015, n)),
        "NOISE_F": 2.2 + np.cumsum(rng.normal(0.0005, 0.013, n)),
    }
    return pd.DataFrame(data, index=idx)


def selftest(args) -> None:
    print("SELF-TEST — synthetic panel, no network calls\n" + "=" * 60)
    log_px = synthetic_panel()
    args.outdir = args.outdir or "pairs_output_selftest"
    res = run(args, log_px=log_px)
    if not res:
        print("\nSelf-test found no tradeable pairs — check the install/parameters.")
        return
    found = {p.name for p in res["book"]}
    print(f"\nPairs found: {sorted(found)}")
    expected = {"COINT_A vs COINT_B", "COINT_C vs COINT_D"}
    print("Recovered a planted cointegrated pair:",
          "yes" if found & expected else "NO — investigate")


# ----------------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Find cointegrated pairs from live market data and backtest them "
                    "with a Kalman-filter walk-forward rolling window.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    u = p.add_argument_group("universe")
    u.add_argument("--index", help=f"one of: {', '.join(sorted(INDEX_SOURCES))}")
    u.add_argument("--tickers", nargs="+", help="explicit Yahoo tickers (2 or more)")
    u.add_argument("--tickers-file", help="text file, one ticker per line")
    u.add_argument("--start", default="2015-01-01")
    u.add_argument("--end", default=None, help="default: today")
    u.add_argument("--max-tickers", type=int, default=60, help="cap the universe size")
    u.add_argument("--min-coverage", type=float, default=0.5,
                   help="minimum fraction of the sample a ticker must have prices for")

    s = p.add_argument_group("pair screen")
    s.add_argument("--screen-frac", type=float, default=0.7,
                   help="fraction of history used to select pairs (rest is clean OOS)")
    s.add_argument("--coint-alpha", type=float, default=0.05)
    s.add_argument("--adf-alpha", type=float, default=0.05)
    s.add_argument("--rolling-window", type=int, default=504,
                   help="stability-test window in days (~2y)")
    s.add_argument("--rolling-step", type=int, default=21)
    s.add_argument("--min-pass-rate", type=float, default=0.5)
    s.add_argument("--min-windows", type=int, default=10,
                   help="minimum independent windows behind a pass rate")
    s.add_argument("--no-stability", action="store_true", help="skip the rolling test")
    s.add_argument("--no-i1", action="store_true",
                   help="skip the ADF I(1) pre-check on each leg (Engle-Granger only)")
    s.add_argument("--max-pairs", type=int, default=2000, help="cap pairs tested")
    s.add_argument("--max-book", type=int, default=10, help="max pairs traded")
    s.add_argument("--max-corr", type=float, default=0.7,
                   help="drop a pair if its spread returns correlate above this with an accepted one")
    s.add_argument("--allow-shared-legs", action="store_true",
                   help="let one stock anchor several pairs")
    s.add_argument("--force", action="store_true",
                   help="with exactly 2 tickers, trade them even if they fail the screen")

    b = p.add_argument_group("signal & walk-forward")
    b.add_argument("--is-window", type=int, default=262, help="context window (days)")
    b.add_argument("--oos-step", type=int, default=63, help="trading window (days)")
    b.add_argument("--entry-z", type=float, default=1.5)
    b.add_argument("--exit-z", type=float, default=0.5)
    b.add_argument("--z-window", type=int, default=60)
    b.add_argument("--obs-cov", type=float, default=1.0, help="Kalman observation covariance R")
    b.add_argument("--delta", type=float, default=1e-4, help="Kalman state drift")
    b.add_argument("--cost-bps", type=float, default=15.0, help="transaction cost per leg")
    b.add_argument("--rf", type=float, default=0.0, help="annual risk-free rate, e.g. 0.04")
    b.add_argument("--bootstrap", type=int, default=1000,
                   help="bootstrap resamples for the Sharpe CI (0 to skip)")

    o = p.add_argument_group("output")
    o.add_argument("--outdir", default="pairs_output")
    o.add_argument("--no-plots", action="store_true")
    o.add_argument("--selftest", action="store_true",
                   help="run on synthetic data, no network required")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    if args.selftest:
        selftest(args)
        return
    run(args)


if __name__ == "__main__":
    main()
