"""System prompts and the rule engine that feeds them.

The advisor is only as good as the context it gets. This module does
three jobs:

1. Reads the ``investor-os/`` markdown files so the philosophy, memory
   and P&L are in front of the model on every call.
2. Runs a deterministic rule engine over the live book — fund overlap,
   concentration, position count, stale losers. Python computes the
   numbers; the model only interprets them. A language model should
   never be asked to do the arithmetic that decides a rule breach.
3. Assembles the prompts for morning brief, market scan, chat, and the
   short Telegram update.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
INVESTOR_OS = ROOT / "investor-os"

# ISIN prefixes typical of pooled funds and ETFs. Used to separate fund
# holdings from direct lines so overlap can be flagged — a fund and a
# single name can be the same underlying exposure counted twice.
FUND_DOMICILES = ("IE", "LU", "JE", "GG", "KY")

# Operating companies incorporated in those jurisdictions carry the same
# ISIN prefix but are not funds. Without this list the check cries wolf.
NOT_A_FUND = {
    "PNR_US_EQ",   # Pentair plc
    "STX_US_EQ",   # Seagate
    "ACN_US_EQ",   # Accenture
    "MDT_US_EQ",   # Medtronic
    "JCI_US_EQ",   # Johnson Controls
    "ETN_US_EQ",   # Eaton
    "TRNE_US_EQ",
    "AON_US_EQ",
    "LIN_US_EQ",
}

# Thresholds from investor-one-pager.md.
SINGLE_POSITION_CEILING_PCT = 10.0
SOCIAL_CEILING_PCT = 2.0
ATTENTION_BUDGET_HOURS = 2.0
MAX_TACTICAL_TRADES_PER_MONTH = 3
CASH_FLOOR_GBP = 8_000.0
COOLDOWN_HOURS_BASELINE = 72
COOLDOWN_DAYS_HYPE = 7


# ------------------------------------------------------- investor-os files


def load_investor_os() -> dict[str, str]:
    """Read the philosophy files. Missing files degrade to empty strings."""
    files = {
        "one_pager": INVESTOR_OS / "investor-one-pager.md",
        "memory": INVESTOR_OS / "memory.md",
        "instructions": INVESTOR_OS / "instructions.md",
        "pnl": INVESTOR_OS / "financials" / "pnl-summary.md",
    }
    out: dict[str, str] = {}
    for key, path in files.items():
        try:
            out[key] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            out[key] = ""
    return out


# ---------------------------------------------------------- rule engine


def _value(holding: dict) -> float:
    return float(holding.get("Current value") or 0.0)


def _pct(holding: dict) -> float:
    return float(holding.get("Unrealised P/L %") or 0.0) * 100.0


def diagnostics(portfolio: dict) -> dict:
    """Deterministic checks against the one-pager rules.

    Returns a dict of computed facts plus a list of breaches. Nothing
    here is a judgement call — every number is arithmetic on the book.
    """
    holdings = portfolio.get("holdings") or []
    summary = portfolio.get("summary") or {}
    total = sum(_value(h) for h in holdings) or 1.0

    # --- fund vs direct exposure --------------------------------------
    funds = [
        h
        for h in holdings
        if (h.get("ISIN") or "")[:2] in FUND_DOMICILES
        and h.get("Ticker") not in NOT_A_FUND
    ]
    fund_value = sum(_value(h) for h in funds)

    # --- concentration -------------------------------------------------
    ranked = sorted(holdings, key=_value, reverse=True)
    top5_value = sum(_value(h) for h in ranked[:5])
    top10_value = sum(_value(h) for h in ranked[:10])
    oversized = [h for h in holdings if _value(h) / total * 100.0 > SINGLE_POSITION_CEILING_PCT]

    # --- the long tail -------------------------------------------------
    tail = [h for h in holdings if _value(h) / total * 100.0 < 0.5]
    tail_value = sum(_value(h) for h in tail)

    # --- losers, and how long they have been held ----------------------
    losers = sorted([h for h in holdings if _pct(h) < 0], key=_pct)
    deep_losers = [h for h in holdings if _pct(h) <= -20.0]

    # --- currency mix --------------------------------------------------
    currency: dict[str, float] = {}
    for holding in holdings:
        code = (holding.get("Instrument currency") or "?").upper()
        code = "GBP" if code == "GBX" else code
        currency[code] = currency.get(code, 0.0) + _value(holding)

    # --- attention math ------------------------------------------------
    minutes_each = (
        (ATTENTION_BUDGET_HOURS * 60.0) / len(holdings) if holdings else 0.0
    )

    breaches: list[dict] = []

    if fund_value / total * 100.0 >= 20.0:
        breaches.append(
            {
                "rule": "Fund / direct-line overlap",
                "severity": "warning",
                "detail": (
                    f"{len(funds)} pooled funds hold GBP {fund_value:,.0f} "
                    f"({fund_value / total * 100:.1f}% of the book). Broad funds overlap "
                    "with the direct lines held alongside them, so true exposure to any "
                    "large constituent is higher than its single-line weight suggests. "
                    "The one-pager requires checking what a fund already holds before "
                    "adding either side."
                ),
                "tickers": [h.get("Ticker") for h in funds],
            }
        )

    for holding in oversized:
        weight = _value(holding) / total * 100.0
        breaches.append(
            {
                "rule": f"Single-position ceiling ({SINGLE_POSITION_CEILING_PCT:.0f}%)",
                "severity": "warning",
                "detail": (
                    f"{holding.get('Name')} is {weight:.1f}% of the book."
                ),
                "tickers": [holding.get("Ticker")],
            }
        )

    if holdings and minutes_each < 5.0:
        breaches.append(
            {
                "rule": "Attention budget (2 hrs/week)",
                "severity": "warning",
                "detail": (
                    f"{len(holdings)} positions against {ATTENTION_BUDGET_HOURS:.0f} hrs/week "
                    f"is {minutes_each:.1f} minutes per holding per week. The one-pager bars "
                    "any position needing more attention than the budget allows."
                ),
                "tickers": [],
            }
        )

    if tail:
        breaches.append(
            {
                "rule": "Attention budget — the long tail",
                "severity": "info",
                "detail": (
                    f"{len(tail)} positions are each under 0.5% of the book, "
                    f"{tail_value / total * 100:.1f}% of value in total. "
                    "They consume most of the position count and almost none of the risk."
                ),
                "tickers": [h.get("Ticker") for h in tail],
            }
        )

    for holding in deep_losers:
        breaches.append(
            {
                "rule": "Stop-loss discipline",
                "severity": "warning",
                "detail": (
                    f"{holding.get('Name')} is {_pct(holding):+.1f}% "
                    f"(opened {holding.get('Opened')}). No stop appears to have fired. "
                    "The one-pager records that the stop rule lapses on emotional positions."
                ),
                "tickers": [holding.get("Ticker")],
            }
        )

    return {
        "as_of": portfolio.get("generatedAt"),
        "total_value": summary.get("Total account value"),
        "invested_value": total,
        "cost_basis": summary.get("Invested (cost basis)"),
        "unrealised_pl": summary.get("Unrealised P/L"),
        "unrealised_pct": (summary.get("Unrealised P/L %") or 0.0) * 100.0,
        "realised_pl": summary.get("Realised P/L (all time)"),
        "cash_in_account": (summary.get("Cash available to trade") or 0.0)
        + (summary.get("Cash in pies") or 0.0),
        "position_count": len(holdings),
        "fund_count": len(funds),
        "fund_value": fund_value,
        "fund_pct": fund_value / total * 100.0,
        "fund_holdings": funds,
        "top5_pct": top5_value / total * 100.0,
        "top10_pct": top10_value / total * 100.0,
        "oversized": oversized,
        "tail_count": len(tail),
        "tail_pct": tail_value / total * 100.0,
        "losers": losers,
        "deep_losers": deep_losers,
        "currency_mix": {k: v / total * 100.0 for k, v in currency.items()},
        "minutes_per_holding": minutes_each,
        "breaches": breaches,
    }


# --------------------------------------------------------------- digests


def portfolio_digest(portfolio: dict, *, limit: int = 25) -> str:
    """Compact, token-efficient text view of the book for the model."""
    holdings = portfolio.get("holdings") or []
    checks = diagnostics(portfolio)
    ranked = sorted(holdings, key=_value, reverse=True)

    lines = [
        f"PORTFOLIO SNAPSHOT (as of {portfolio.get('generatedAt', 'unknown')})",
        f"Total account value: GBP {checks['total_value']:,.2f}",
        f"Cost basis: GBP {checks['cost_basis']:,.2f}",
        f"Unrealised P/L: GBP {checks['unrealised_pl']:,.2f} "
        f"({checks['unrealised_pct']:+.2f}%)",
        f"Realised P/L (all time): GBP {checks['realised_pl']:,.2f}",
        f"Cash inside the brokerage account: GBP {checks['cash_in_account']:,.2f}",
        f"Positions: {checks['position_count']}",
        f"Top 5 concentration: {checks['top5_pct']:.1f}% | "
        f"Top 10: {checks['top10_pct']:.1f}%",
        "",
        f"TOP {min(limit, len(ranked))} HOLDINGS BY VALUE:",
    ]

    for i, holding in enumerate(ranked[:limit], 1):
        lines.append(
            f"{i:>2}. {holding.get('Name', '')[:34]:<34} "
            f"{holding.get('Ticker', ''):<14} "
            f"GBP {_value(holding):>9,.0f} "
            f"{float(holding.get('Weight %') or 0) * 100:>5.2f}% "
            f"P/L {_pct(holding):>+7.1f}%  since {holding.get('Opened', '')}"
        )

    if len(ranked) > limit:
        rest = sum(_value(h) for h in ranked[limit:])
        lines.append(
            f"    ... plus {len(ranked) - limit} smaller positions "
            f"totalling GBP {rest:,.0f}"
        )

    lines += ["", "CURRENCY MIX:"]
    for code, pct in sorted(
        checks["currency_mix"].items(), key=lambda kv: kv[1], reverse=True
    ):
        lines.append(f"  {code}: {pct:.1f}%")

    return "\n".join(lines)


def diagnostics_digest(checks: dict) -> str:
    """Rule-engine output as text for the model to interpret."""
    if not checks["breaches"]:
        return "RULE ENGINE: no breaches detected."

    lines = ["RULE ENGINE — COMPUTED BREACHES (arithmetic, not opinion):"]
    order = {"critical": 0, "warning": 1, "info": 2}
    for breach in sorted(checks["breaches"], key=lambda b: order.get(b["severity"], 9)):
        lines.append(f"[{breach['severity'].upper()}] {breach['rule']}")
        lines.append(f"    {breach['detail']}")
    return "\n".join(lines)


def quotes_digest(quotes: dict) -> str:
    """Live market data as text."""
    if not quotes:
        return ""
    lines = ["LIVE MARKET DATA:"]
    for label, quote in quotes.items():
        if not getattr(quote, "ok", False) or quote.price is None:
            lines.append(f"  {label}: unavailable ({getattr(quote, 'error', '')})")
            continue
        move = (
            f"{quote.change_pct:+.2f}%" if quote.change_pct is not None else "n/a"
        )
        extra = ""
        if quote.pct_off_52w_high is not None:
            extra = f", {quote.pct_off_52w_high:+.1f}% vs 52w high"
        if quote.pe_ratio:
            extra += f", P/E {quote.pe_ratio:.1f}"
        lines.append(
            f"  {label} ({quote.symbol}): {quote.price:,.2f} {quote.currency} "
            f"{move}{extra}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------- prompts


def system_prompt(portfolio: dict, *, include_full_files: bool = True) -> str:
    """The standing brief. Every call gets this."""
    files = load_investor_os()
    checks = diagnostics(portfolio)

    philosophy = files["one_pager"] if include_full_files else files["one_pager"][:6000]
    context = files["memory"] if include_full_files else files["memory"][:4000]

    return f"""You are Ivan's personal investment strategist, operating to the standard of a senior UBS client advisor. Probing, direct, no fluff, no flattery.

You are NOT a licensed financial adviser. You frame decisions; you never make them. You do not tell Ivan to buy or sell. You surface what the rules say, what the numbers say, and what the trade-off is — then he decides.

Today is {date.today().isoformat()}.

════════════════════════════════════════════════════════
HOW YOU OPERATE
════════════════════════════════════════════════════════

1. The one-pager below is canonical. Run every idea against it and name the specific rule before anything else.
2. Numbers come from the RULE ENGINE block, which is computed in Python. Never recompute or estimate portfolio arithmetic yourself — quote the engine.
3. Surface contradictions between what Ivan says and what the book shows. That gap is the single most valuable thing you provide.
4. No preamble, no recap, no flattery. Lead with the finding.
5. End with the next question or the next action — never a summary.
6. **Tax is out of scope.** Never raise tax, capital gains, tax residency, domicile, wrappers or cross-border tax treatment, and never factor them into sizing, entry, exit or allocation reasoning. If asked directly, say it is outside what this system covers and point him to a professional. This is deliberate.
7. Employer compliance and personal account dealing rules ARE in scope — they govern what he is permitted to trade.

STANDING CHECKS — apply before responding to any trade idea, sizing question, or "what do you think of X":

• HYPE CHECK — Ivan's named trigger is hype. If the idea arrived via momentum, a news cycle, a listing event or social chatter, name that first and make him justify it from fundamentals.
• MARGIN CHECK — Ivan runs a separate CFD/options/leveraged-ETF account. Ask whether leverage is involved. Assume it is until he says otherwise.
• STOP-LOSS CHECK — his stop rule lapses on positions he is emotional about. If a position is underwater, ask where the stop was and why it did not fire.
• IDLE-TIME CHECK — from September 2026 he is unemployed with full market access. Elevated overtrading risk. Ceiling is {MAX_TACTICAL_TRADES_PER_MONTH} tactical trades per month.
• COMPLIANCE CHECK — job-hunting into finance. Employer personal account dealing rules may prohibit shorts, margin and short-horizon trading. Raise before any tactical idea. This is regulatory, not tax.
• OVERLAP CHECK — 31.8% of the book sits in broad pooled funds held alongside direct lines in the same companies. True exposure to any large name exceeds its single-line weight. Check the fund's holdings before adding either side.
• REASSURANCE CHECK — under stress he seeks reassurance, and reassurance turns into action. If he is asking about a losing position, give analysis, not comfort.
• INERTIA CHECK — zero disposals in twelve months. His risk is not panic-selling; it is holding losers indefinitely and calling it conviction.

════════════════════════════════════════════════════════
INVESTOR ONE-PAGER (canonical)
════════════════════════════════════════════════════════
{philosophy}

════════════════════════════════════════════════════════
PERSONAL CONTEXT & MEMORY
════════════════════════════════════════════════════════
{context}

════════════════════════════════════════════════════════
LIVE PORTFOLIO
════════════════════════════════════════════════════════
{portfolio_digest(portfolio)}

════════════════════════════════════════════════════════
{diagnostics_digest(checks)}
════════════════════════════════════════════════════════
"""


def morning_brief_prompt(quotes: dict | None = None) -> str:
    market = quotes_digest(quotes or {})
    return f"""Write this morning's brief for Ivan.

{market}

Structure it exactly as follows, and keep the whole thing under 400 words:

**POSITION** — one sentence on where the book stands. Value, unrealised P/L, and the single number that matters most today.

**WHAT MOVED** — the two or three positions or market levels that actually changed. Skip anything that did not move meaningfully. If nothing moved, say so in one line rather than manufacturing content.

**RULE WATCH** — the highest-severity open breach from the rule engine, and what specifically would close it. One breach, not a list. Rotate to a different one on different days rather than repeating the same item daily.

**ONE QUESTION** — the single sharpest question Ivan should sit with today. Not a task, a question.

Do not recommend buying or selling anything. Do not pad. If a section has nothing worth saying, write one line saying that."""


def market_scan_prompt(quotes: dict, positions: Iterable[dict]) -> str:
    names = ", ".join(
        f"{p.get('Name')} ({p.get('Ticker')})" for p in positions
    )
    return f"""Analyse Ivan's five largest positions using the live data below.

Positions: {names}

{quotes_digest(quotes)}

For each of the five, give:
• **Where it sits** — weight in the book, unrealised P/L, and how long held.
• **What the market data says** — price versus 52-week high, valuation if meaningful, momentum.
• **The rule that applies** — concentration ceiling, fund overlap, attention budget, stop discipline. Name it explicitly.
• **The honest read** — one sentence. Is this a considered position or an inherited one?

Then close with **THE ONE THING**: across all five, the single most consequential issue, and the trade-off Ivan faces in addressing it. State the trade-off on both sides — do not tell him what to do.

Under 700 words. Tables where they help."""


def telegram_prompt(quotes: dict | None = None) -> str:
    market = quotes_digest(quotes or {})
    return f"""Write Ivan's daily Telegram update. It is read on a phone, half-awake, before work.

{market}

Hard constraints:
• Under 180 words total. This is the most important constraint.
• Four short blocks, each 1–2 lines: BOOK / MOVED / WATCH / QUESTION.
• Plain sentences. No markdown tables, no headers, no bullet characters beyond a simple dash.
• Numbers must come from the rule engine and market data above.
• One rule-watch item only, and rotate which one across days.
• No buy or sell recommendations.

If nothing meaningful happened, say that in one line. A short honest message beats a padded one."""


def chat_prompt(question: str, history: list[dict] | None = None) -> str:
    """Wrap a user question with recent conversation for continuity."""
    if not history:
        return question
    lines = ["Recent conversation, for continuity:"]
    for message in history[-10:]:
        role = "Ivan" if message["role"] == "user" else "You"
        body = message["content"]
        if len(body) > 500:
            body = body[:500] + "..."
        lines.append(f"{role}: {body}")
    lines += ["", f"Ivan's question now: {question}"]
    return "\n".join(lines)


if __name__ == "__main__":
    from prices import load_portfolio

    book = load_portfolio()
    checks = diagnostics(book)
    print(portfolio_digest(book, limit=10))
    print()
    print(diagnostics_digest(checks))
