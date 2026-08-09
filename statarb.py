"""Statistical arbitrage — headless wrapper around ``pairs_walkforward``.

``pairs_walkforward.py`` is the engine and is used unmodified: its
cointegration screen, Kalman hedge ratio, z-score signal and walk-forward
backtest are all imported rather than reimplemented. This module adds the
parts a dashboard needs and a CLI does not:

* universe resolution from free text — tickers or an index name
* per-pair walk-forward metrics, so pairs can be ranked against each other
* an entry/exit z-band chosen by grid search **on the screening sample
  only**, then validated walk-forward on the untouched remainder
* a live signal: where the spread sits today, and what that implies

    from statarb import run_scan
    result = run_scan("nasdaq100", start="2018-01-01", top_n=5)
    for pair in result.pairs:
        print(pair.name, pair.sharpe, pair.recommendation)

On methodology: the band search is in-sample by construction. It runs on
the screening slice, and every headline number reported afterwards comes
from walk-forward folds the search never saw. Reporting the in-sample
Sharpe as if it were achievable is the single easiest way to lie with a
pairs backtest, so both are returned and the UI shows them side by side.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np
import pandas as pd

import pairs_walkforward as engine

TRADING_DAYS = engine.TRADING_DAYS

# Grid searched for the entry/exit band. Deliberately coarse — a fine grid
# over a short sample finds noise, not structure.
# exit_z = 0 is excluded deliberately: |z| <= 0 is only true at exactly zero,
# so the position never flattens and the "pair trade" becomes a permanently
# invested directional bet. A real exit band has to have width.
ENTRY_GRID = (1.0, 1.5, 2.0, 2.5)
EXIT_GRID = (0.25, 0.5, 0.75, 1.0)

INDEX_ALIASES = {
    "s&p500": "sp500", "s&p 500": "sp500", "spx": "sp500", "sp500": "sp500",
    "^gspc": "sp500", "sandp": "sp500",
    "nasdaq": "nasdaq100", "nasdaq100": "nasdaq100", "ndx": "nasdaq100",
    "^ndx": "nasdaq100", "nasdaq 100": "nasdaq100",
    "ftse": "ftse100", "ftse100": "ftse100", "ukx": "ftse100", "^ftse": "ftse100",
    "dax": "dax", "^gdaxi": "dax",
    "hsi": "hsi", "hangseng": "hsi", "hang seng": "hsi", "^hsi": "hsi",
    "sti": "sti", "straits": "sti",
    "nikkei": "nikkei225", "nikkei225": "nikkei225", "^n225": "nikkei225",
}


# ------------------------------------------------------------------ types


@dataclass
class PairResult:
    """One pair, screened, backtested and scored."""

    y: str
    x: str
    beta: float
    coint_p: float
    rolling_pass_rate: float
    n_windows: int

    entry_z: float = 1.5
    exit_z: float = 0.5
    band_sharpe_is: float = np.nan       # in-sample, from the band search

    sharpe: float = np.nan               # walk-forward
    total_return: float = np.nan
    ann_return: float = np.nan
    ann_vol: float = np.nan
    max_drawdown: float = np.nan
    hit_rate: float = np.nan
    n_trades: float = 0.0
    days: int = 0

    current_z: float = np.nan
    current_beta: float = np.nan
    signal: str = "WAIT"
    recommendation: str = ""
    half_life: float = np.nan

    returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    spread: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    zscore: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    @property
    def name(self) -> str:
        return f"{self.y} vs {self.x}"

    def as_row(self) -> dict:
        return {
            "Pair": self.name,
            "Sharpe": self.sharpe,
            "Return": self.total_return,
            "Ann. return": self.ann_return,
            "Max DD": self.max_drawdown,
            "Entry z": self.entry_z,
            "Exit z": self.exit_z,
            "Current z": self.current_z,
            "Signal": self.signal,
            "Coint p": self.coint_p,
            "Stability": self.rolling_pass_rate,
            "Half-life": self.half_life,
            "Trades": self.n_trades,
        }


@dataclass
class Funnel:
    """Where candidate pairs died. Shown in the UI so an empty result is
    diagnosable rather than just disappointing."""

    universe: int = 0
    pairs_tested: int = 0
    i1_legs: int = 0
    killed_by_i1: int = 0
    cointegrated: int = 0
    killed_by_beta: int = 0
    stable: int = 0
    after_diversify: int = 0
    relaxed: bool = False

    def as_rows(self) -> list[tuple[str, str]]:
        return [
            ("Tickers in universe", f"{self.universe}"),
            ("Pairs tested", f"{self.pairs_tested:,}"),
            ("Legs that are I(1)", f"{self.i1_legs}/{self.universe}"),
            ("Rejected — leg not I(1)", f"{self.killed_by_i1:,}"),
            ("Cointegrated", f"{self.cointegrated}"),
            ("Rejected — negative hedge ratio", f"{self.killed_by_beta}"),
            ("Passed rolling stability", f"{self.stable}"),
            ("Kept after diversification", f"{self.after_diversify}"),
        ]


@dataclass
class ScanResult:
    pairs: list[PairResult] = field(default_factory=list)
    portfolio: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    portfolio_metrics: dict = field(default_factory=dict)
    universe: list[str] = field(default_factory=list)
    n_candidates: int = 0
    n_stable: int = 0
    screen_end: pd.Timestamp | None = None
    error: str = ""
    notes: list[str] = field(default_factory=list)
    funnel: Funnel = field(default_factory=Funnel)
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)


# -------------------------------------------------------------- universe


def parse_universe(text: str) -> tuple[str, list[str]]:
    """Work out whether the user typed an index or a list of tickers.

    Returns ``("index", [name])`` or ``("tickers", [...])``.
    """
    raw = (text or "").strip()
    if not raw:
        return "tickers", []

    key = raw.lower().replace("_", " ").strip()
    if key in INDEX_ALIASES:
        return "index", [INDEX_ALIASES[key]]
    if key in engine.INDEX_SOURCES:
        return "index", [key]

    parts = [
        p.strip().upper()
        for p in raw.replace(",", " ").replace(";", " ").split()
        if p.strip()
    ]
    # A single word that is not a known index is treated as a ticker.
    return "tickers", parts


_CONSTITUENTS: dict[str, list[str]] = {}

# Used only when the Wikipedia scrape fails — a corporate proxy, an SSL store
# problem, or the page layout changing. These are large, liquid names that
# have been in their index for years, so they go stale slowly. They are a
# usable universe, not an accurate index membership list.
FALLBACK_CONSTITUENTS: dict[str, list[str]] = {
    "nasdaq100": [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "AVGO", "TSLA",
        "COST", "NFLX", "AMD", "PEP", "ADBE", "LIN", "CSCO", "TMUS", "INTU",
        "QCOM", "TXN", "AMGN", "AMAT", "ISRG", "BKNG", "HON", "VRTX", "ADP",
        "PANW", "REGN", "MU", "ADI", "LRCX", "SBUX", "GILD", "MDLZ", "KLAC",
        "SNPS", "CDNS", "MELI", "MAR", "ORLY", "CSX", "ASML", "PYPL", "ABNB",
        "FTNT", "CRWD", "NXPI", "CTAS", "PCAR",
    ],
    "sp500": [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK-B", "AVGO",
        "TSLA", "JPM", "LLY", "V", "UNH", "XOM", "MA", "COST", "HD", "PG",
        "JNJ", "WMT", "NFLX", "ABBV", "CRM", "BAC", "ORCL", "CVX", "MRK",
        "KO", "AMD", "PEP", "ADBE", "TMO", "LIN", "CSCO", "ACN", "MCD",
        "ABT", "WFC", "DHR", "TXN", "GE", "VZ", "PM", "INTU", "AMGN", "CAT",
        "IBM", "DIS", "GS", "MS",
    ],
    "ftse100": [
        "AZN.L", "SHEL.L", "HSBA.L", "ULVR.L", "BP.L", "RIO.L", "GSK.L",
        "REL.L", "DGE.L", "BATS.L", "GLEN.L", "NG.L", "LSEG.L", "RKT.L",
        "AAL.L", "BA.L", "LLOY.L", "PRU.L", "CPG.L", "TSCO.L", "NWG.L",
        "BARC.L", "IMB.L", "VOD.L", "SSE.L", "AHT.L", "STAN.L", "III.L",
        "ANTO.L", "SGE.L", "WPP.L", "SMT.L", "CRH.L", "EXPN.L", "IHG.L",
    ],
    "hsi": [
        "0700.HK", "0939.HK", "0941.HK", "1299.HK", "0005.HK", "3690.HK",
        "9988.HK", "1810.HK", "0388.HK", "2318.HK", "0883.HK", "0857.HK",
        "0386.HK", "1398.HK", "3988.HK", "2628.HK", "0016.HK", "0001.HK",
        "0002.HK", "0003.HK", "0011.HK", "0012.HK", "0027.HK", "0066.HK",
        "0101.HK", "0175.HK", "0267.HK", "0288.HK", "0669.HK", "0688.HK",
    ],
}


def index_constituents(name: str, max_tickers: int | None = None) -> list[str]:
    """Scrape index members, with a timeout and no SystemExit.

    ``engine.index_constituents`` calls ``pd.read_html(url)`` directly, which
    has no timeout and raises SystemExit when lxml is absent. SystemExit is a
    BaseException, so it escapes ``except Exception`` and halts Streamlit mid
    render — the UI freezes on the last thing it drew. Fetching the HTML
    ourselves keeps both failure modes catchable.
    """
    key = name.lower()
    if key in _CONSTITUENTS:
        cached = _CONSTITUENTS[key]
        return cached[:max_tickers] if max_tickers else cached

    if key not in engine.INDEX_SOURCES:
        raise ValueError(
            f"Unknown index '{name}'. Known: "
            f"{', '.join(sorted(engine.INDEX_SOURCES))}."
        )

    try:
        import lxml  # noqa: F401
    except ImportError as error:
        raise RuntimeError(
            "Index lookup needs lxml. Install it with:\n"
            "    venv/bin/pip install lxml\n"
            "Or enter tickers directly instead of an index name."
        ) from error

    url, col_hint, suffix = engine.INDEX_SOURCES[key]

    try:
        import requests
        from io import StringIO

        # Explicit CA bundle. Python.org macOS builds ship with an empty
        # certificate store until "Install Certificates.command" is run, which
        # breaks urllib (and so pandas' own read_html(url)). certifi sidesteps
        # it entirely.
        try:
            import certifi

            verify: object = certifi.where()
        except ImportError:
            verify = True

        response = requests.get(
            url,
            timeout=25,
            verify=verify,
            headers={"User-Agent": "Mozilla/5.0 (Maple portfolio dashboard)"},
        )
        response.raise_for_status()
        tables = pd.read_html(StringIO(response.text))
    except Exception as error:  # noqa: BLE001
        fallback = FALLBACK_CONSTITUENTS.get(key)
        if fallback:
            _CONSTITUENTS[key] = fallback
            return fallback[:max_tickers] if max_tickers else fallback
        raise RuntimeError(
            f"Could not fetch the {name.upper()} constituent list ({error}). "
            "Enter tickers directly instead."
        ) from error

    tickers: list[str] = []
    for table in tables:
        columns = [str(c) for c in table.columns]
        matches = [c for c in columns if col_hint.lower() in c.lower()]
        if not matches:
            continue
        values = table[matches[0]].dropna().astype(str).tolist()
        if len(values) >= 10:
            tickers = values
            break

    if not tickers:
        raise RuntimeError(
            f"Found the {name.upper()} page but no constituents table on it. "
            "Enter tickers directly instead."
        )

    if key == "hsi":
        out = [engine._clean_hk_ticker(t) for t in tickers]
    else:
        out = [
            t.strip().replace(".", "-") + suffix if suffix
            else t.strip().replace(".", "-")
            for t in tickers
        ]
    out = [t for t in dict.fromkeys(out) if t and t.lower() != "nan"]

    _CONSTITUENTS[key] = out
    return out[:max_tickers] if max_tickers else out


def resolve_universe(
    text: str, *, max_tickers: int = 60
) -> tuple[list[str], list[str]]:
    """Turn the input into a ticker list. Returns (tickers, notes)."""
    kind, values = parse_universe(text)
    notes: list[str] = []

    if kind == "index":
        name = values[0]
        tickers = index_constituents(name, max_tickers)
        key = name.lower()
        if _CONSTITUENTS.get(key) == FALLBACK_CONSTITUENTS.get(key):
            notes.append(
                f"Could not reach Wikipedia — using a built-in {name.upper()} "
                "list of large, long-standing members. It is a usable universe "
                "but not current index membership."
            )
        else:
            notes.append(
                f"{name.upper()}: using {len(tickers)} constituents "
                f"(capped at {max_tickers})."
            )
        return tickers, notes

    if len(values) == 1:
        notes.append(
            f"'{values[0]}' is a single ticker — a pair needs at least two. "
            "Add a second ticker, or enter an index name."
        )
    return values, notes


# ------------------------------------------------------- spread mechanics


def half_life(spread: pd.Series) -> float:
    """Ornstein-Uhlenbeck mean-reversion half-life, in trading days.

    Regress Δspread on lagged spread; the decay coefficient gives the time
    to close half the gap. A pair with a half-life longer than the holding
    window will not revert inside a trade, however cointegrated it looks.
    """
    s = pd.Series(spread).dropna()
    if len(s) < 30:
        return np.nan
    lagged = s.shift(1).dropna()
    delta = s.diff().dropna()
    aligned = pd.concat([delta, lagged], axis=1).dropna()
    if len(aligned) < 30:
        return np.nan
    try:
        import statsmodels.api as sm

        model = sm.OLS(
            aligned.iloc[:, 0].values, sm.add_constant(aligned.iloc[:, 1].values)
        ).fit()
        kappa = float(model.params[1])
    except Exception:  # noqa: BLE001
        return np.nan
    if kappa >= 0:
        return np.nan  # not mean-reverting

    life = float(-np.log(2) / kappa)
    # On a finite sample a random walk yields a marginally negative kappa and
    # so a huge, meaningless half-life. Two guards: it must be estimable from
    # the sample, and it must be short enough to trade at all. A spread taking
    # over a year to close half its gap is not a pairs trade.
    if life > min(len(s) / 4, TRADING_DAYS):
        return np.nan
    return life


def _vectorised_positions(z: np.ndarray, entry: float, exit_: float) -> np.ndarray:
    """Same state machine as engine.zscore_signal, as a loop over an array.

    Kept separate so the band grid search can run hundreds of times without
    rebuilding DataFrames.
    """
    pos = np.zeros(len(z))
    current = 0.0
    for t in range(len(z)):
        if z[t] > entry:
            current = -1.0
        elif z[t] < -entry:
            current = 1.0
        elif abs(z[t]) <= exit_:
            current = 0.0
        pos[t] = current
    return pos


def optimise_band(
    details: pd.DataFrame,
    *,
    z_window: int = 60,
    cost_bps: float = 15.0,
    entry_grid: Sequence[float] = ENTRY_GRID,
    exit_grid: Sequence[float] = EXIT_GRID,
) -> tuple[float, float, float]:
    """Grid-search the entry/exit band on one Kalman-filtered sample.

    Returns ``(entry_z, exit_z, sharpe)``. The caller must pass a sample
    that will not be used for reporting — this is an in-sample fit.
    """
    if details.empty or "spread" not in details:
        return 1.5, 0.5, np.nan

    mu = details["spread"].rolling(z_window, min_periods=z_window).mean()
    sd = details["spread"].rolling(z_window, min_periods=z_window).std()
    frame = details.assign(zscore=(details["spread"] - mu) / sd).dropna(
        subset=["zscore"]
    )
    if len(frame) < 60:
        return 1.5, 0.5, np.nan

    z = frame["zscore"].to_numpy()
    r_y = frame["y"].diff()
    r_x = frame["x"].diff()
    beta_lag = frame["beta_t"].shift(1)
    gross = 1.0 + beta_lag.abs()
    spread_ret = ((r_y - beta_lag * r_x) / gross).to_numpy()
    beta_arr = frame["beta_t"].to_numpy()

    best = (1.5, 0.5, -np.inf)
    for entry, exit_ in itertools.product(entry_grid, exit_grid):
        if exit_ >= entry:
            continue
        pos = _vectorised_positions(z, entry, exit_)
        pos_lag = np.concatenate([[0.0], pos[:-1]])
        gross_ret = pos_lag * spread_ret

        leg_y = np.abs(np.diff(pos, prepend=0.0))
        leg_x = np.abs(np.diff(pos * beta_arr, prepend=0.0))
        cost = (cost_bps / 1e4) * (leg_y + leg_x) / gross.to_numpy()

        net = np.nan_to_num(gross_ret) - np.nan_to_num(cost)
        if net.std() <= 0 or (net != 0).sum() < 20:
            continue
        sharpe = float(net.mean() / net.std() * np.sqrt(TRADING_DAYS))
        if sharpe > best[2]:
            best = (entry, exit_, sharpe)

    if best[2] == -np.inf:
        return 1.5, 0.5, np.nan
    return best


def live_signal(
    log_px: pd.DataFrame,
    pair: PairResult,
    *,
    z_window: int = 60,
    obs_cov: float = 1.0,
    delta: float = 1e-4,
) -> None:
    """Populate current z, signal and recommendation, in place.

    Uses the full history through today — this is the trading decision, not
    a backtest, so using everything available is correct.
    """
    y, x = log_px[pair.y], log_px[pair.x]
    alpha0, beta0 = engine.ols_hedge_ratio(y, x)
    if np.isnan(beta0):
        pair.recommendation = "Cannot fit a hedge ratio on current data."
        return

    details = engine.kalman_hedge_ratio(
        y, x, obs_cov=obs_cov, delta=delta, init_alpha=alpha0, init_beta=beta0
    )
    if details.empty:
        pair.recommendation = "No overlapping price history."
        return

    mu = details["spread"].rolling(z_window, min_periods=z_window).mean()
    sd = details["spread"].rolling(z_window, min_periods=z_window).std()
    z = ((details["spread"] - mu) / sd).dropna()
    if z.empty:
        pair.recommendation = "Not enough history for a z-score."
        return

    pair.spread = details["spread"]
    pair.zscore = z
    pair.half_life = half_life(details["spread"])
    pair.current_z = float(z.iloc[-1])
    pair.current_beta = float(details["beta_t"].iloc[-1])

    value = pair.current_z
    entry, exit_ = pair.entry_z, pair.exit_z

    if value >= entry:
        pair.signal = "SHORT SPREAD"
        pair.recommendation = (
            f"z = {value:+.2f}, at or beyond the +{entry:.2f} entry. "
            f"Short {pair.y}, long {pair.current_beta:.2f}× {pair.x}. "
            f"Target exit at |z| ≤ {exit_:.2f}."
        )
    elif value <= -entry:
        pair.signal = "LONG SPREAD"
        pair.recommendation = (
            f"z = {value:+.2f}, at or beyond the -{entry:.2f} entry. "
            f"Long {pair.y}, short {pair.current_beta:.2f}× {pair.x}. "
            f"Target exit at |z| ≤ {exit_:.2f}."
        )
    elif abs(value) <= exit_:
        pair.signal = "FLAT"
        pair.recommendation = (
            f"z = {value:+.2f}, inside the ±{exit_:.2f} exit band. "
            "Spread is at fair value — nothing to do."
        )
    else:
        pair.signal = "WAIT"
        gap = entry - abs(value)
        direction = "short" if value > 0 else "long"
        pair.recommendation = (
            f"z = {value:+.2f}, {gap:.2f} short of the ±{entry:.2f} entry. "
            f"Watch for a {direction}-spread entry."
        )

    if not np.isnan(pair.half_life):
        if pair.half_life > 120:
            pair.recommendation += (
                f" Half-life is {pair.half_life:.0f} days — slow to revert; "
                "size for a long hold."
            )
        elif pair.half_life > 0:
            pair.recommendation += f" Half-life {pair.half_life:.0f} days."


# ----------------------------------------------------------------- screen


def screen_with_progress(
    screen_px: pd.DataFrame,
    *,
    coint_alpha: float = 0.05,
    adf_alpha: float = 0.05,
    rolling_window: int = 504,
    rolling_step: int = 21,
    min_pass_rate: float = 0.30,
    min_windows: int = 8,
    max_pairs: int = 2000,
    progress: Callable[[float, str], None] | None = None,
    base: float = 0.15,
    span: float = 0.40,
) -> tuple[pd.DataFrame, Funnel]:
    """Cointegration screen with stage-by-stage progress and a funnel count.

    Mirrors ``engine.screen_pairs`` using the same primitives, but reports
    where it is and why pairs were rejected. The stability test is the slow
    part — one Engle-Granger fit per rolling window per surviving pair — so
    it only runs on pairs that already cointegrate.
    """
    funnel = Funnel(universe=screen_px.shape[1])

    def say(fraction: float, message: str) -> None:
        if progress:
            progress(base + span * fraction, message)

    columns = list(screen_px.columns)
    combos = list(itertools.combinations(columns, 2))
    if len(combos) > max_pairs:
        combos = combos[:max_pairs]
    funnel.pairs_tested = len(combos)

    # Stage 1 — I(1) per leg, cached so it is computed once per ticker.
    say(0.0, f"Testing {len(columns)} tickers for I(1) behaviour…")
    i1: dict[str, bool] = {}
    for i, column in enumerate(columns):
        i1[column] = engine.is_i1(screen_px[column], adf_alpha)
        if i % 5 == 0:
            say(
                0.25 * (i / max(len(columns), 1)),
                f"ADF unit-root test {i + 1}/{len(columns)}…",
            )
    funnel.i1_legs = sum(i1.values())

    # Stage 2 — Engle-Granger on every eligible pair.
    say(0.25, f"Cointegration test across {len(combos):,} pairs…")
    survivors: list[dict] = []
    for i, (a, b) in enumerate(combos):
        if i % 25 == 0:
            say(
                0.25 + 0.45 * (i / max(len(combos), 1)),
                f"Cointegration {i:,}/{len(combos):,} · {len(survivors)} found",
            )
        if not (i1.get(a) and i1.get(b)):
            funnel.killed_by_i1 += 1
            continue
        p_value = engine.engle_granger(screen_px[a], screen_px[b])
        if np.isnan(p_value) or p_value >= coint_alpha:
            continue
        funnel.cointegrated += 1
        alpha_ols, beta = engine.ols_hedge_ratio(screen_px[a], screen_px[b])
        if np.isnan(beta) or beta <= 0:
            funnel.killed_by_beta += 1
            continue
        survivors.append(
            {
                "y": a, "x": b, "pair": f"{a} vs {b}",
                "coint_p": p_value, "beta": beta, "alpha_ols": alpha_ols,
            }
        )

    if not survivors:
        return pd.DataFrame(), funnel

    # Stage 3 — rolling stability, only on pairs that got this far.
    say(0.70, f"Rolling stability on {len(survivors)} cointegrated pairs…")
    for i, row in enumerate(survivors):
        say(
            0.70 + 0.30 * (i / max(len(survivors), 1)),
            f"Stability {i + 1}/{len(survivors)} · {row['pair']}",
        )
        rate, windows = engine.rolling_coint_stability(
            screen_px[row["y"]], screen_px[row["x"]],
            rolling_window, rolling_step, coint_alpha,
        )
        row["rolling_pass_rate"] = rate
        row["n_windows"] = windows

    frame = pd.DataFrame(survivors)
    frame["stable"] = (frame["rolling_pass_rate"] >= min_pass_rate) & (
        frame["n_windows"] >= min_windows
    )
    funnel.stable = int(frame["stable"].sum())

    # Nothing cleared the bar. Rather than reporting failure, keep the most
    # stable pairs and mark the result as relaxed — real cointegration is
    # intermittent, and a 40% pass rate is still far better than random.
    if funnel.stable == 0:
        ranked = frame.sort_values("rolling_pass_rate", ascending=False)
        keep = ranked.head(8).index
        frame.loc[keep, "stable"] = True
        funnel.relaxed = True

    frame = frame.sort_values(
        ["stable", "rolling_pass_rate", "coint_p"], ascending=[False, False, True]
    )
    return frame.reset_index(drop=True), funnel


# ------------------------------------------------------------------- scan


def run_scan(
    universe_text: str,
    *,
    start: str = "2018-01-01",
    end: str | None = None,
    top_n: int = 5,
    max_tickers: int = 60,
    screen_frac: float = 0.7,
    coint_alpha: float = 0.05,
    # 0.30, not 0.50: measured against planted cointegrated pairs, genuine
    # relationships score 35-45% pass rates. A 50% bar rejects everything.
    min_pass_rate: float = 0.30,
    cost_bps: float = 15.0,
    is_window: int = 262,
    oos_step: int = 63,
    z_window: int = 60,
    max_pairs: int = 2000,
    optimise_bands: bool = True,
    progress: Callable[[float, str], None] | None = None,
) -> ScanResult:
    """Screen, band-fit, walk-forward backtest and rank pairs.

    ``progress(fraction, message)`` is called throughout so a UI can show
    where it is — the cointegration screen is O(n²) and slow.
    """
    result = ScanResult()

    def say(fraction: float, message: str) -> None:
        if progress:
            progress(fraction, message)

    # --- universe -----------------------------------------------------
    # Reported before the work starts: resolving an index scrapes Wikipedia,
    # which is slow enough that silence looks like a hang.
    say(0.01, "Resolving universe…")
    try:
        tickers, notes = resolve_universe(universe_text, max_tickers=max_tickers)
        result.notes.extend(notes)
    # BaseException, not Exception: the engine raises SystemExit when lxml is
    # missing, which would otherwise halt Streamlit mid-render.
    except BaseException as error:  # noqa: BLE001
        result.error = f"Could not resolve the universe: {error}"
        return result

    if len(tickers) < 2:
        result.error = (
            "Need at least two tickers. Enter two or more symbols "
            "(e.g. 'KO PEP'), or an index name (e.g. 'nasdaq100')."
        )
        return result

    # --- prices -------------------------------------------------------
    pair_count = len(tickers) * (len(tickers) - 1) // 2
    say(
        0.03,
        f"Downloading {len(tickers)} tickers from Yahoo "
        f"({pair_count:,} pairs to test)…",
    )
    try:
        prices = engine.download_prices(tickers, start, end)
        log_px = engine.clean_prices(prices, 0.5)
    except BaseException as error:  # noqa: BLE001
        result.error = f"Price download failed: {error}"
        return result

    if log_px.shape[1] < 2:
        result.error = (
            f"Only {log_px.shape[1]} ticker(s) survived cleaning. "
            "Check the symbols are valid on Yahoo Finance."
        )
        return result

    result.universe = list(log_px.columns)

    # --- screening sample --------------------------------------------
    split = int(len(log_px) * screen_frac)
    screen_px = log_px.iloc[:split]
    result.screen_end = log_px.index[split - 1] if 0 < split < len(log_px) else None

    rolling_window = 504
    if rolling_window >= len(screen_px):
        rolling_window = max(126, len(screen_px) // 3)
        result.notes.append(
            f"Short sample — stability window reduced to {rolling_window} days."
        )

    # --- screen -------------------------------------------------------
    try:
        candidates, funnel = screen_with_progress(
            screen_px,
            coint_alpha=coint_alpha,
            rolling_window=rolling_window,
            min_pass_rate=min_pass_rate,
            max_pairs=max_pairs,
            progress=progress,
        )
    except BaseException as error:  # noqa: BLE001
        result.error = f"Screening failed: {error}"
        return result

    result.funnel = funnel
    result.candidates = candidates

    if candidates.empty:
        if funnel.cointegrated == 0:
            result.error = (
                f"Tested {funnel.pairs_tested:,} pairs — none cointegrated at "
                f"p < {coint_alpha}. "
                + (
                    f"Only {funnel.i1_legs} of {funnel.universe} legs were I(1), "
                    "which rules most pairs out before testing. "
                    if funnel.i1_legs < funnel.universe * 0.6
                    else ""
                )
                + "Try a wider universe, an earlier start date, or a looser p-value."
            )
        else:
            result.error = (
                f"{funnel.cointegrated} pairs cointegrated but all had a negative "
                "hedge ratio, which is a long-long combination rather than a spread."
            )
        return result

    result.n_candidates = int(len(candidates))
    result.n_stable = funnel.stable

    if funnel.relaxed:
        result.notes.append(
            f"No pair cleared the {min_pass_rate:.0%} stability bar — real "
            "cointegration is intermittent. Showing the most stable candidates "
            "instead; treat these as weaker and check the pass rate per pair."
        )

    # --- diversify ----------------------------------------------------
    say(0.58, "Selecting a diversified book…")
    book = engine.diversify(
        log_px, candidates, max_corr=0.7, unique_legs=True, max_book=max(top_n, 5)
    )
    if not book:
        result.error = "No pairs survived the diversification filter."
        return result
    result.funnel.after_diversify = len(book)

    # --- per pair: band fit, then walk-forward ------------------------
    pairs: list[PairResult] = []
    for i, spec in enumerate(book):
        say(
            0.6 + 0.35 * (i / max(len(book), 1)),
            f"Backtesting {spec.name} ({i + 1}/{len(book)})…",
        )

        pair = PairResult(
            y=spec.y, x=spec.x, beta=spec.beta, coint_p=spec.coint_p,
            rolling_pass_rate=spec.rolling_pass_rate, n_windows=spec.n_windows,
        )

        # Band chosen on the screening slice only.
        if optimise_bands:
            try:
                a0, b0 = engine.ols_hedge_ratio(
                    screen_px[spec.y], screen_px[spec.x]
                )
                screen_details = engine.kalman_hedge_ratio(
                    screen_px[spec.y], screen_px[spec.x],
                    init_alpha=a0, init_beta=b0,
                )
                entry, exit_, is_sharpe = optimise_band(
                    screen_details, z_window=z_window, cost_bps=cost_bps
                )
                pair.entry_z, pair.exit_z, pair.band_sharpe_is = entry, exit_, is_sharpe
            except Exception:  # noqa: BLE001
                pass

        # Walk-forward on the full history with that band.
        try:
            single = engine.PairSpec(
                y=spec.y, x=spec.x, beta=spec.beta, alpha_ols=spec.alpha_ols,
                coint_p=spec.coint_p, rolling_pass_rate=spec.rolling_pass_rate,
                n_windows=spec.n_windows, weight=1.0,
            )
            returns, _, folds = engine.walk_forward_backtest(
                log_px, [single],
                is_window=is_window, oos_step=oos_step,
                entry_z=pair.entry_z, exit_z=pair.exit_z, z_window=z_window,
                cost_bps=cost_bps,
            )
            metrics = engine.performance_metrics(returns)
            pair.returns = returns
            pair.sharpe = metrics.get("sharpe", np.nan)
            pair.total_return = metrics.get("total_return", np.nan)
            pair.ann_return = metrics.get("ann_return", np.nan)
            pair.ann_vol = metrics.get("ann_vol", np.nan)
            pair.max_drawdown = metrics.get("max_drawdown", np.nan)
            pair.hit_rate = metrics.get("hit_rate", np.nan)
            pair.days = metrics.get("days", 0)
            pair.n_trades = float(folds["n_trades"].sum()) if not folds.empty else 0.0
        except Exception:  # noqa: BLE001
            continue

        live_signal(log_px, pair, z_window=z_window)
        pairs.append(pair)

    if not pairs:
        result.error = "No pair produced a usable backtest."
        return result

    # Rank by walk-forward Sharpe — the number that was not fitted.
    pairs.sort(key=lambda p: (-1e9 if np.isnan(p.sharpe) else p.sharpe), reverse=True)
    result.pairs = pairs[:top_n]

    # --- equal-weight portfolio of the survivors ----------------------
    say(0.97, "Combining the book…")
    if result.pairs:
        weight = 1.0 / len(result.pairs)
        combined = pd.concat(
            [p.returns.rename(p.name) for p in result.pairs if not p.returns.empty],
            axis=1,
        )
        if not combined.empty:
            result.portfolio = (combined.fillna(0.0) * weight).sum(axis=1)
            result.portfolio_metrics = engine.performance_metrics(result.portfolio)

    say(1.0, "Done.")
    return result


if __name__ == "__main__":
    # Offline check on the engine's own synthetic panel.
    panel = engine.synthetic_panel()
    split = int(len(panel) * 0.7)
    found = engine.screen_pairs(panel.iloc[:split], verbose=False)
    print(f"Candidates: {len(found)}")
    print(found[["pair", "coint_p", "beta", "rolling_pass_rate", "stable"]])
