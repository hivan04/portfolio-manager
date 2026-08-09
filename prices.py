"""Live market data.

Stocks and ETFs come from yfinance; crypto from CoinGecko. Trading 212
uses its own ticker format (``NVDA_US_EQ``, ``HSBAl_EQ``), so the first
job here is translating those into symbols Yahoo recognises.

    from prices import quote_portfolio, load_portfolio
    portfolio = load_portfolio()
    quotes = quote_portfolio(portfolio["holdings"][:5])

Everything degrades gracefully: a failed lookup returns a Quote with
``ok=False`` and an error string rather than raising, because a dashboard
that dies on one bad ticker is worse than one showing 4 of 5 prices.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
PORTFOLIO_JSON = ROOT / "data" / "portfolio.json"

COINGECKO = "https://api.coingecko.com/api/v3"

# Trading 212 suffix -> Yahoo exchange suffix.
EXCHANGE_SUFFIX = {
    "l": ".L",      # London
    "d": ".DE",     # Xetra
    "s": ".SW",     # SIX Swiss
    "a": ".AS",     # Amsterdam
    "p": ".PA",     # Paris
    "m": ".MI",     # Milan
    "e": ".MC",     # Madrid
    "US": "",       # US listings need no suffix
}

# Cases the pattern cannot derive: renames, dual listings, oddities.
TICKER_OVERRIDES = {
    "FB_US_EQ": "META",      # Meta kept the old T212 ticker
    "RBSl_EQ": "NWG.L",      # RBS renamed to NatWest Group
    "GOOGL_US_EQ": "GOOGL",
    "BB3Ml_EQ": "BB3M.L",
    "EMHGl_EQ": "EMHG.L",
    "IHCUl_EQ": "IHCU.L",
    "VUAGl_EQ": "VUAG.L",
    "SEMIl_EQ": "SEMI.L",
    "UBSGs_EQ": "UBSG.SW",
    "ALVd_EQ": "ALV.DE",
}

# Symbols quoted in pence rather than pounds on Yahoo.
PENCE_QUOTED = re.compile(r"\.L$")

# Common crypto aliases -> CoinGecko ids. The portfolio holds none today
# (crypto is in the anti-portfolio), but the plumbing is here so a future
# holding or a watchlist entry works without a code change.
CRYPTO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
}

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300.0  # seconds


# ------------------------------------------------------------------ types


@dataclass
class Quote:
    """One instrument's live market state."""

    symbol: str
    name: str = ""
    price: float | None = None
    currency: str = ""
    previous_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    pct_off_52w_high: float | None = None
    market_cap: float | None = None
    pe_ratio: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    sector: str = ""
    asset_type: str = "equity"
    ok: bool = True
    error: str = ""
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------- ticker map


def t212_to_yahoo(ticker: str) -> str:
    """Translate a Trading 212 ticker into a Yahoo Finance symbol.

    ``NVDA_US_EQ`` -> ``NVDA``,  ``HSBAl_EQ`` -> ``HSBA.L``,
    ``ALVd_EQ`` -> ``ALV.DE``,   ``UBSGs_EQ`` -> ``UBSG.SW``.
    """
    if not ticker:
        return ""
    if ticker in TICKER_OVERRIDES:
        return TICKER_OVERRIDES[ticker]

    stem = ticker[:-3] if ticker.endswith("_EQ") else ticker

    if stem.endswith("_US"):
        return stem[:-3]

    # Trailing lowercase letter marks a non-US exchange.
    match = re.match(r"^([A-Z0-9.]+)([a-z])$", stem)
    if match:
        base, marker = match.groups()
        return f"{base}{EXCHANGE_SUFFIX.get(marker, '')}"

    return stem


def _cached(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    stamp, value = hit
    if (datetime.now(timezone.utc).timestamp() - stamp) > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return value


def _store(key: str, value):
    _CACHE[key] = (datetime.now(timezone.utc).timestamp(), value)
    return value


def clear_cache() -> None:
    _CACHE.clear()


# ------------------------------------------------------------------ equity


def get_quote(symbol: str, *, use_cache: bool = True) -> Quote:
    """Fetch one equity/ETF quote from Yahoo Finance."""
    symbol = (symbol or "").strip()
    if not symbol:
        return Quote(symbol="", ok=False, error="empty symbol")

    key = f"eq:{symbol}"
    if use_cache and (hit := _cached(key)) is not None:
        return hit

    try:
        import yfinance as yf
    except ImportError:
        return Quote(symbol=symbol, ok=False, error="yfinance not installed")

    try:
        ticker = yf.Ticker(symbol)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:  # noqa: BLE001 - yfinance raises many shapes
            info = {}

        price = _first_number(
            info.get("currentPrice"),
            info.get("regularMarketPrice"),
            info.get("previousClose"),
        )
        previous = _first_number(
            info.get("regularMarketPreviousClose"), info.get("previousClose")
        )

        # Fall back to recent history when .info is thin (common for
        # LSE-listed ETFs).
        if price is None:
            frame = ticker.history(period="5d", auto_adjust=False)
            if not frame.empty:
                price = float(frame["Close"].iloc[-1])
                if len(frame) > 1:
                    previous = float(frame["Close"].iloc[-2])

        if price is None:
            return _store(
                key, Quote(symbol=symbol, ok=False, error="no price returned")
            )

        currency = (info.get("currency") or "").upper()
        # Yahoo quotes LSE lines in pence; normalise to pounds so the
        # dashboard never mixes units.
        divisor = 100.0 if (currency == "GBP" and PENCE_QUOTED.search(symbol)) else 1.0
        if currency == "GBp":
            divisor = 100.0

        price = price / divisor
        previous = previous / divisor if previous else None

        change = (price - previous) if previous else None
        change_pct = (change / previous * 100.0) if (change and previous) else None

        high52 = _first_number(info.get("fiftyTwoWeekHigh"))
        if high52:
            high52 /= divisor
        low52 = _first_number(info.get("fiftyTwoWeekLow"))
        if low52:
            low52 /= divisor

        quote = Quote(
            symbol=symbol,
            name=info.get("shortName") or info.get("longName") or symbol,
            price=price,
            currency="GBP" if divisor == 100.0 else (currency or ""),
            previous_close=previous,
            change=change,
            change_pct=change_pct,
            day_high=_scale(info.get("dayHigh"), divisor),
            day_low=_scale(info.get("dayLow"), divisor),
            week52_high=high52,
            week52_low=low52,
            pct_off_52w_high=(
                (price - high52) / high52 * 100.0 if high52 else None
            ),
            market_cap=_first_number(info.get("marketCap")),
            pe_ratio=_first_number(info.get("trailingPE"), info.get("forwardPE")),
            volume=_first_number(info.get("volume"), info.get("regularMarketVolume")),
            avg_volume=_first_number(info.get("averageVolume")),
            sector=info.get("sector") or info.get("category") or "",
            asset_type="etf" if info.get("quoteType") == "ETF" else "equity",
        )
        return _store(key, quote)

    except Exception as error:  # noqa: BLE001
        return _store(key, Quote(symbol=symbol, ok=False, error=str(error)[:200]))


def _scale(value, divisor: float) -> float | None:
    number = _first_number(value)
    return number / divisor if number is not None else None


def _first_number(*values) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def get_history(symbol: str, period: str = "6mo", interval: str = "1d"):
    """Price history as a DataFrame. Empty frame on failure."""
    key = f"hist:{symbol}:{period}:{interval}"
    if (hit := _cached(key)) is not None:
        return hit
    try:
        import yfinance as yf

        frame = yf.Ticker(symbol).history(period=period, interval=interval)
        if not frame.empty and PENCE_QUOTED.search(symbol):
            for column in ("Open", "High", "Low", "Close"):
                if column in frame:
                    frame[column] = frame[column] / 100.0
        return _store(key, frame)
    except Exception:  # noqa: BLE001
        import pandas as pd

        return _store(key, pd.DataFrame())


# ------------------------------------------------------------------ crypto


def get_crypto_quote(asset: str, vs_currency: str = "gbp") -> Quote:
    """Fetch a crypto quote from CoinGecko.

    Accepts a symbol (``BTC``) or a CoinGecko id (``bitcoin``).
    """
    asset = (asset or "").strip()
    if not asset:
        return Quote(symbol="", ok=False, error="empty asset", asset_type="crypto")

    coin_id = CRYPTO_IDS.get(asset.upper(), asset.lower())
    key = f"cg:{coin_id}:{vs_currency}"
    if (hit := _cached(key)) is not None:
        return hit

    try:
        import requests

        response = requests.get(
            f"{COINGECKO}/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": vs_currency,
                "include_24hr_change": "true",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json().get(coin_id)
        if not payload:
            return _store(
                key,
                Quote(
                    symbol=asset.upper(),
                    ok=False,
                    error=f"unknown CoinGecko id '{coin_id}'",
                    asset_type="crypto",
                ),
            )

        price = float(payload.get(vs_currency) or 0.0)
        change_pct = payload.get(f"{vs_currency}_24h_change")

        quote = Quote(
            symbol=asset.upper(),
            name=coin_id.replace("-", " ").title(),
            price=price,
            currency=vs_currency.upper(),
            change_pct=float(change_pct) if change_pct is not None else None,
            change=(
                price * float(change_pct) / 100.0 if change_pct is not None else None
            ),
            market_cap=payload.get(f"{vs_currency}_market_cap"),
            volume=payload.get(f"{vs_currency}_24h_vol"),
            asset_type="crypto",
        )
        return _store(key, quote)

    except Exception as error:  # noqa: BLE001
        return _store(
            key,
            Quote(
                symbol=asset.upper(),
                ok=False,
                error=str(error)[:200],
                asset_type="crypto",
            ),
        )


# --------------------------------------------------------------- portfolio


def load_portfolio(path: Path | str = PORTFOLIO_JSON) -> dict:
    """Read the Trading 212 snapshot written by ``t212.sync``."""
    path = Path(path)
    if not path.exists():
        return {"generatedAt": None, "summary": {}, "holdings": [], "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return {
            "generatedAt": None,
            "summary": {},
            "holdings": [],
            "history": [],
            "error": str(error),
        }


def top_positions(holdings: Iterable[dict], n: int = 5) -> list[dict]:
    """The n largest holdings by current value."""
    return sorted(
        holdings, key=lambda h: float(h.get("Current value") or 0.0), reverse=True
    )[:n]


def quote_portfolio(holdings: Iterable[dict]) -> dict[str, Quote]:
    """Quote a set of Trading 212 holdings, keyed by T212 ticker."""
    results: dict[str, Quote] = {}
    for holding in holdings:
        ticker = holding.get("Ticker") or ""
        symbol = t212_to_yahoo(ticker)
        if not symbol:
            results[ticker] = Quote(
                symbol="", ok=False, error=f"cannot map ticker '{ticker}'"
            )
            continue
        quote = get_quote(symbol)
        if not quote.name or quote.name == symbol:
            quote.name = holding.get("Name") or symbol
        results[ticker] = quote
    return results


def market_context(vs_currency: str = "gbp") -> dict[str, Quote]:
    """Index and macro backdrop for the morning brief."""
    benchmarks = {
        "S&P 500": "^GSPC",
        "Nasdaq 100": "^NDX",
        "FTSE 100": "^FTSE",
        "VIX": "^VIX",
        "US 10Y": "^TNX",
        "Gold": "GC=F",
        "Brent": "BZ=F",
        "GBP/USD": "GBPUSD=X",
    }
    return {label: get_quote(symbol) for label, symbol in benchmarks.items()}


if __name__ == "__main__":
    book = load_portfolio()
    print(f"Loaded {len(book['holdings'])} holdings\n")
    for position in top_positions(book["holdings"], 5):
        mapped = t212_to_yahoo(position["Ticker"])
        print(f"{position['Ticker']:<16} -> {mapped:<10} {position['Name']}")
