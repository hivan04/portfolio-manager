"""Model wrapper — the advisor itself. Backed by Google Gemini.

Four entry points:

    analyzer = Analyzer()
    analyzer.morning_brief()             # full brief for the dashboard
    analyzer.market_scan()               # deep read of the top 5 positions
    analyzer.chat("why is semis 21%?")   # conversational, remembers context
    analyzer.telegram_morning_update()   # short brief, sent to phone

Every call is given the investor-os philosophy, the live book, and the
deterministic rule-engine output. The model interprets; it never does the
portfolio arithmetic.

Needs ``GOOGLE_API_KEY`` in ``keys/.env`` or the environment — get one
free at https://aistudio.google.com/apikey — EXCEPT for the offline path.
Because the rule engine already computes every number, a useful brief can
be assembled in pure Python:

    analyzer.morning_brief(offline=True)
    analyzer.telegram_morning_update(offline=True)

The offline brief loses the prose and the judgement. It keeps the
figures, the rule watch, and a rotating question — most of what a daily
brief is for. It costs nothing and cannot fail on a bad API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import ui.prompts as prompts
from venv.memory import Memory
from ui.prices import (
    load_portfolio,
    market_context,
    quote_portfolio,
    top_positions,
)

ROOT = Path(__file__).resolve().parent.parent
ENV_CANDIDATES = (ROOT / "keys" / ".env", ROOT / ".env")

# Model selection is resolved at runtime, not hardcoded. Google retires
# model names on a rolling basis — pinning one guarantees a 404 in a few
# months. "flash" and "pro" are tiers, not names: the resolver asks the
# API what this key can actually use and picks the newest match.
FAST_TIER = "flash"   # cheap, fast — the daily brief and chat
DEEP_TIER = "pro"     # slower, better reasoning — the market scan

DEFAULT_MODEL = FAST_TIER
DEEP_MODEL = DEEP_TIER

# Accept either name so an existing GEMINI_API_KEY also works.
KEY_NAMES = ("GOOGLE_API_KEY", "GEMINI_API_KEY")

# Skip these unless nothing else is available — unstable or special-purpose.
_AVOID = ("preview", "exp", "thinking", "image", "audio", "tts", "embedding",
          "vision", "live", "learnlm", "gemma")

_MODEL_CACHE: dict[str, list[str]] = {}


def _version_key(name: str) -> tuple:
    """Sort key that puts newer Gemini versions first.

    ``gemini-3-flash`` outranks ``gemini-2.5-flash``; ``-latest`` aliases
    outrank dated snapshots because they keep working.
    """
    import re

    match = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    major = int(match.group(1)) if match else 0
    minor = int(match.group(2) or 0) if match else 0
    is_alias = 1 if name.endswith("-latest") else 0
    # Dated snapshots (…-001, …-09-2025) are fine but rank below plain names.
    is_plain = 0 if re.search(r"-\d{3}$|-\d{2}-\d{4}$", name) else 1
    return (major, minor, is_alias, is_plain, name)


def load_env() -> None:
    """Load every .env we know about, not just the first one found.

    Stopping at the first file meant a key added to the project-root
    .env was ignored whenever keys/.env already existed.
    """
    for path in ENV_CANDIDATES:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key() -> str:
    load_env()
    for name in KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value and not value.startswith("your_"):
            return value

    # Streamlit Cloud has no keys/.env — secrets set in the app's Settings
    # panel land in st.secrets, not the environment, so check there too.
    try:
        import streamlit as st

        for name in KEY_NAMES:
            value = str(st.secrets.get(name, "")).strip()
            if value and not value.startswith("your_"):
                return value
    except Exception:  # noqa: BLE001 - no secrets configured, or not running under Streamlit
        pass

    return ""


# Rotating prompts for the offline brief. One per day, cycled so the
# brief does not read identically every morning. Drawn from the
# one-pager's live contradictions and open queue.
QUESTIONS = [
    "Which position would you not buy again today at today's price? What stops you selling it?",
    "You have 1.6 minutes per holding per week. Which 40 of the 74 could you close and not miss?",
    "VUAG is 16% and you hold Apple, Microsoft and Nvidia separately. How much is doubled up?",
    "Figma has been down ~78% for twelve months. What specifically are you waiting for?",
    "AI and adjacent is 31% of the book. Did you choose that, or did it accumulate?",
    "Name the last position you sold. If nothing comes to mind, that is the finding.",
    "If your income is zero from September, what does that change about how you size?",
    "Top 5 is 40.9% of the book. Which one would hurt most at half its price?",
    "You bought 47 positions in one afternoon. What was the thesis, in one line, for any of them?",
    "Which rule in the one-pager have you broken most recently, and did you notice at the time?",
    "The SpaceX short — where is the stop, and why has it not fired?",
    "Semis are 21.5%. Is that conviction, or three separate hype entries stacking up?",
    "What would have to be true for you to cut the position count below 30 this month?",
    "You have never sold anything. What is your actual sell criterion, written as a sentence?",
]


def _delta_line(current: float | None, previous: float | None, label: str) -> str:
    if current is None or previous is None:
        return f"{label}: no prior snapshot to compare against."
    change = current - previous
    pct = (change / previous * 100.0) if previous else 0.0
    direction = "up" if change > 0 else ("down" if change < 0 else "flat")
    return f"{label}: {direction} £{abs(change):,.0f} ({pct:+.2f}%)"


@dataclass
class Response:
    """One model reply, with enough metadata to debug a bad one."""

    text: str
    ok: bool = True
    error: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def __str__(self) -> str:
        return self.text


class Analyzer:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        memory: Memory | None = None,
        portfolio: dict | None = None,
        max_tokens: int = 2000,
    ) -> None:
        load_env()
        self.model = model
        self.max_tokens = max_tokens
        self.memory = memory or Memory()
        self._portfolio = portfolio
        self._client = None

    # ---------------------------------------------------------- plumbing

    @property
    def portfolio(self) -> dict:
        if self._portfolio is None:
            self._portfolio = load_portfolio()
        return self._portfolio

    def refresh_portfolio(self) -> dict:
        """Re-read the snapshot from disk and record it in the database."""
        self._portfolio = load_portfolio()
        if self._portfolio.get("holdings"):
            self.memory.save_snapshot(self._portfolio)
        return self._portfolio

    @property
    def client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as error:
                raise RuntimeError(
                    "google-genai not installed — run: pip install google-genai"
                ) from error
            key = api_key()
            if not key:
                raise RuntimeError(
                    "GOOGLE_API_KEY missing. Add it to keys/.env — "
                    "get one at https://aistudio.google.com/apikey"
                )
            self._client = genai.Client(api_key=key)
        return self._client

    def is_configured(self) -> bool:
        """True only if the key exists AND the SDK is importable here.

        Both matter: a key with no SDK in the running interpreter fails
        just as hard as no key at all, and the offline paths should take
        over in either case rather than raising.
        """
        if not api_key():
            return False
        try:
            import google.genai  # noqa: F401
        except ImportError:
            return False
        return True

    # ------------------------------------------------------ model choice

    def available_models(self, *, refresh: bool = False) -> list[str]:
        """Every model this key can call generateContent on, newest first."""
        cache_key = api_key()[-8:] or "none"
        if not refresh and cache_key in _MODEL_CACHE:
            return _MODEL_CACHE[cache_key]

        names: list[str] = []
        try:
            for model in self.client.models.list():
                actions = getattr(model, "supported_actions", None) or []
                if actions and "generateContent" not in actions:
                    continue
                name = (model.name or "").replace("models/", "")
                if name:
                    names.append(name)
        except Exception:  # noqa: BLE001 - offline, bad key, quota
            return []

        names.sort(key=_version_key, reverse=True)
        _MODEL_CACHE[cache_key] = names
        return names

    def resolve_model(self, wanted: str) -> str:
        """Turn a tier ("flash"/"pro") or an exact name into a live model.

        An exact name is used as-is if the API confirms it exists. If it
        does not — the usual cause of a 404 after Google retires a name —
        we fall back to the newest model in the same tier.
        """
        wanted = (wanted or FAST_TIER).strip()
        models = self.available_models()

        if not models:
            # Cannot enumerate. Hand back what was asked and let the call
            # surface a real error rather than inventing a name.
            return wanted

        if wanted in models:
            return wanted

        tier = DEEP_TIER if DEEP_TIER in wanted.lower() else FAST_TIER
        preferred = [
            name
            for name in models
            if tier in name.lower()
            and name.startswith("gemini")
            and not any(bad in name.lower() for bad in _AVOID)
        ]
        if preferred:
            return preferred[0]

        # Nothing stable in that tier. Keep the tier before dropping it —
        # a preview flash beats a stable pro when flash is what was asked for.
        same_tier = [
            name for name in models if tier in name.lower() and name.startswith("gemini")
        ]
        if same_tier:
            return same_tier[0]

        stable = [
            name
            for name in models
            if name.startswith("gemini")
            and not any(bad in name.lower() for bad in _AVOID)
        ]
        return stable[0] if stable else models[0]

    def _call(
        self,
        user_prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        model: str | None = None,
        _retrying: bool = False,
    ) -> Response:
        try:
            from google.genai import types
        except ImportError:
            import sys

            return Response(
                text="",
                ok=False,
                error=(
                    "google-genai is not installed in the interpreter running this "
                    f"app ({sys.executable}). Install it there:\n\n"
                    f"    {sys.executable} -m pip install google-genai\n\n"
                    "If you have a virtualenv, start Streamlit from it explicitly: "
                    "venv/bin/streamlit run app.py"
                ),
                model=model or self.model,
            )

        chosen = self.resolve_model(model or self.model)
        system = prompts.system_prompt(self.portfolio)

        try:
            result = self.client.models.generate_content(
                model=chosen,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens or self.max_tokens,
                    temperature=temperature,
                ),
            )

            text = (result.text or "").strip()
            if not text:
                reason = ""
                if getattr(result, "candidates", None):
                    reason = str(getattr(result.candidates[0], "finish_reason", ""))
                return Response(
                    text="",
                    ok=False,
                    error=f"empty response from {chosen} ({reason or 'no reason given'})",
                    model=chosen,
                )

            usage = getattr(result, "usage_metadata", None)
            return Response(
                text=text,
                model=chosen,
                input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            )

        except Exception as error:  # noqa: BLE001
            message = str(error)
            # Google retires model names without warning. If that is what
            # happened, re-read the live list and retry once on whatever
            # this key can actually use.
            if ("NOT_FOUND" in message or "404" in message) and not _retrying:
                self.available_models(refresh=True)
                replacement = self.resolve_model(
                    DEEP_TIER if DEEP_TIER in chosen.lower() else FAST_TIER
                )
                if replacement != chosen:
                    return self._call(
                        user_prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        model=replacement,
                        _retrying=True,
                    )
            return Response(text="", ok=False, error=message[:400], model=chosen)

    # ------------------------------------------------------ offline brief

    def compose_offline_brief(
        self, *, short: bool = False, with_market_data: bool = True
    ) -> str:
        """Build a brief in pure Python. No API key, no model call.

        The rule engine already computes every number the brief needs.
        This assembles them into the same four blocks the model writes —
        POSITION / WHAT MOVED / RULE WATCH / ONE QUESTION — minus the
        interpretation. ``short=True`` gives the Telegram-length version.
        """
        checks = self.diagnostics()
        today = date.today()
        lines: list[str] = []

        # --- POSITION -------------------------------------------------
        value = checks["total_value"] or 0.0
        pl = checks["unrealised_pl"] or 0.0
        pct = checks["unrealised_pct"] or 0.0

        previous = self.memory.snapshot_on_or_before(
            (today - timedelta(days=1)).isoformat()
        )
        prior_value = previous.get("total_value") if previous else None

        lines.append("BOOK" if short else "**POSITION**")
        lines.append(
            f"£{value:,.0f} · unrealised £{pl:+,.0f} ({pct:+.2f}%) · "
            f"{checks['position_count']} positions"
        )
        if prior_value and abs(prior_value - value) > 0.005:
            lines.append(_delta_line(value, prior_value, "Since last snapshot"))

        # --- WHAT MOVED -----------------------------------------------
        lines.append("")
        lines.append("MOVED" if short else "**WHAT MOVED**")

        movers: list[str] = []
        if with_market_data:
            try:
                positions = top_positions(self.portfolio.get("holdings", []), 5)
                quotes = quote_portfolio(positions)
                scored = []
                for holding in positions:
                    quote = quotes.get(holding.get("Ticker", ""))
                    if quote and quote.ok and quote.change_pct is not None:
                        scored.append((abs(quote.change_pct), holding, quote))
                scored.sort(key=lambda row: row[0], reverse=True)

                for _, holding, quote in scored[: (2 if short else 3)]:
                    if abs(quote.change_pct) < 0.4:
                        continue
                    movers.append(
                        f"{holding.get('Name', '')[:28]} {quote.change_pct:+.2f}% "
                        f"({quote.price:,.2f} {quote.currency})"
                    )

                if not short:
                    context = market_context()
                    index_moves = [
                        f"{label} {q.change_pct:+.2f}%"
                        for label, q in context.items()
                        if q.ok and q.change_pct is not None and abs(q.change_pct) >= 0.3
                    ]
                    if index_moves:
                        movers.append("Backdrop: " + ", ".join(index_moves[:4]))
            except Exception as error:  # noqa: BLE001
                movers.append(f"Market data unavailable ({str(error)[:60]})")

        lines.extend(movers if movers else ["Nothing moved meaningfully."])

        # --- RULE WATCH ------------------------------------------------
        lines.append("")
        lines.append("WATCH" if short else "**RULE WATCH**")

        breaches = checks.get("breaches") or []
        if not breaches:
            lines.append("No open breaches.")
        else:
            order = {"critical": 0, "warning": 1, "info": 2}
            ranked = sorted(breaches, key=lambda b: order.get(b["severity"], 9))
            criticals = [b for b in ranked if b["severity"] == "critical"]
            others = [b for b in ranked if b["severity"] != "critical"]
            day = today.timetuple().tm_yday

            chosen = criticals[0] if criticals else None
            if chosen is None and others:
                chosen = others[day % len(others)]
            elif criticals and others and day % 3 == 0:
                # Every third day surface a non-critical, so warnings do
                # not sit permanently hidden behind the top-ranked flag.
                chosen = others[day % len(others)]

            if chosen:
                lines.append(f"[{chosen['severity'].upper()}] {chosen['rule']}")
                detail = chosen["detail"]
                if short and len(detail) > 180:
                    detail = detail[:177].rsplit(" ", 1)[0] + "…"
                lines.append(detail)
            if len(breaches) > 1:
                lines.append(f"({len(breaches)} open in total — see the Rules tab.)")

        # --- ONE QUESTION ----------------------------------------------
        lines.append("")
        lines.append("QUESTION" if short else "**ONE QUESTION**")
        lines.append(QUESTIONS[today.toordinal() % len(QUESTIONS)])

        if not short:
            lines.append("")
            lines.append(
                "_Composed offline from the rule engine — figures only, no "
                "interpretation. Add GOOGLE_API_KEY for the written brief._"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------ actions

    def morning_brief(
        self, *, with_market_data: bool = True, offline: bool = False
    ) -> Response:
        """The full brief shown on the dashboard.

        Falls back to the offline composer when no key is present, so the
        dashboard always has something to show.
        """
        if offline or not self.is_configured():
            text = self.compose_offline_brief(with_market_data=with_market_data)
            self.memory.save_brief(text, channel="app-offline")
            return Response(text=text, model="offline")

        quotes = {}
        if with_market_data:
            quotes = market_context()
            positions = top_positions(self.portfolio.get("holdings", []), 5)
            for ticker, quote in quote_portfolio(positions).items():
                name = next(
                    (p.get("Name") for p in positions if p.get("Ticker") == ticker),
                    ticker,
                )
                quotes[name] = quote

        response = self._call(
            prompts.morning_brief_prompt(quotes), max_tokens=1200, temperature=0.4
        )
        if response.ok:
            self.memory.save_brief(response.text, channel="app")
        return response

    def market_scan(self, *, n: int = 5, deep: bool = False) -> Response:
        """Deep read of the n largest positions against live market data."""
        positions = top_positions(self.portfolio.get("holdings", []), n)
        if not positions:
            return Response(
                text="", ok=False, error="no holdings in data/portfolio.json"
            )
        if not self.is_configured():
            return Response(
                text="",
                ok=False,
                error="GOOGLE_API_KEY missing — the market scan needs a model. "
                "The Market tab still shows live prices without it.",
            )

        quotes = {}
        for ticker, quote in quote_portfolio(positions).items():
            name = next(
                (p.get("Name") for p in positions if p.get("Ticker") == ticker), ticker
            )
            quotes[name] = quote
        quotes.update(market_context())

        return self._call(
            prompts.market_scan_prompt(quotes, positions),
            max_tokens=2500,
            temperature=0.3,
            model=DEEP_MODEL if deep else None,
        )

    def chat(self, question: str, *, session_id: str = "default") -> Response:
        """Conversational turn, with history for continuity."""
        question = (question or "").strip()
        if not question:
            return Response(text="", ok=False, error="empty question")
        if not self.is_configured():
            return Response(
                text="", ok=False, error="GOOGLE_API_KEY missing. Add it to keys/.env"
            )

        history = self.memory.recent_messages(session_id=session_id, limit=20)
        self.memory.add_message("user", question, session_id=session_id)

        response = self._call(
            prompts.chat_prompt(question, history), max_tokens=2000, temperature=0.4
        )
        if response.ok:
            self.memory.add_message("assistant", response.text, session_id=session_id)
        return response

    def telegram_morning_update(
        self, *, send_now: bool = True, force: bool = False, offline: bool = False
    ) -> dict:
        """Short brief for the phone. Safe to call from a cron job.

        Returns a result dict rather than raising, so a scheduled run never
        dies noisily. Guards against double-sending on the same day unless
        ``force=True``. Falls back to the offline composer with no key, so
        the daily message still arrives.
        """
        import ui.telegram_alert as telegram_alert

        if not force and self.memory.brief_sent_today(channel="telegram"):
            return {
                "ok": True,
                "skipped": True,
                "reason": "already sent today",
                "text": "",
            }

        self.refresh_portfolio()

        if offline or not self.is_configured():
            body_text = self.compose_offline_brief(short=True)
            source = "offline"
        else:
            quotes = market_context()
            positions = top_positions(self.portfolio.get("holdings", []), 5)
            for ticker, quote in quote_portfolio(positions).items():
                name = next(
                    (p.get("Name") for p in positions if p.get("Ticker") == ticker),
                    ticker,
                )
                quotes[name] = quote

            response = self._call(
                prompts.telegram_prompt(quotes), max_tokens=600, temperature=0.4
            )
            if not response.ok:
                # A model failure should not cost him the daily message.
                body_text = self.compose_offline_brief(short=True)
                source = f"offline (model failed: {response.error[:80]})"
            else:
                body_text = response.text
                source = response.model

        header = f"Maple — {date.today().strftime('%a %d %b %Y')}\n\n"
        body = header + body_text

        if not send_now:
            return {"ok": True, "skipped": False, "sent": False, "source": source,
                    "text": body}

        result = telegram_alert.send(body)
        if result["ok"]:
            self.memory.save_brief(body, channel="telegram")
        return {
            "ok": result["ok"],
            "skipped": False,
            "sent": result["ok"],
            "source": source,
            "error": result.get("error", ""),
            "text": body,
        }

    # -------------------------------------------------------- convenience

    def diagnostics(self) -> dict:
        """Rule-engine output. No model call, no API key needed."""
        return prompts.diagnostics(self.portfolio)


def main() -> int:
    """CLI entry point — this is what the daily scheduler runs.

        python analyzer.py telegram            # send the morning update
        python analyzer.py telegram --offline  # no model, no key needed
        python analyzer.py brief               # print the full brief
        python analyzer.py scan --deep         # top-5 scan on Gemini Pro
        python analyzer.py check               # rule engine only
        python analyzer.py models              # what this key can call
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Maple advisor.")
    parser.add_argument(
        "command",
        choices=["telegram", "brief", "scan", "check", "models"],
        nargs="?",
        default="check",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--deep", action="store_true", help=f"use {DEEP_MODEL}")
    parser.add_argument("--force", action="store_true", help="ignore the daily guard")
    parser.add_argument("--offline", action="store_true", help="no model call")
    parser.add_argument("--dry-run", action="store_true", help="compose, do not send")
    args = parser.parse_args()

    analyzer = Analyzer(model=args.model)

    if args.command == "check":
        checks = analyzer.diagnostics()
        print(prompts.portfolio_digest(analyzer.portfolio, limit=10))
        print()
        print(prompts.diagnostics_digest(checks))
        return 0

    if args.command == "models":
        if not analyzer.is_configured():
            print("GOOGLE_API_KEY / GEMINI_API_KEY missing. Add it to keys/.env")
            return 1
        models = analyzer.available_models(refresh=True)
        if not models:
            print("Could not list models — check the key and your connection.")
            return 1
        print(f"{len(models)} models available to this key (newest first):\n")
        for name in models:
            marks = []
            if name == analyzer.resolve_model(FAST_TIER):
                marks.append("← fast tier (brief, chat)")
            if name == analyzer.resolve_model(DEEP_TIER):
                marks.append("← deep tier (--deep scan)")
            print(f"  {name:<46} {' '.join(marks)}")
        return 0

    if args.command == "telegram":
        result = analyzer.telegram_morning_update(
            send_now=not args.dry_run, force=args.force, offline=args.offline
        )
        print(json.dumps({k: v for k, v in result.items() if k != "text"}, indent=2))
        if result.get("text"):
            print("\n" + result["text"])
        return 0 if result["ok"] else 1

    if args.command == "brief":
        response = analyzer.morning_brief(offline=args.offline)
    else:
        response = analyzer.market_scan(deep=args.deep)

    if not response.ok:
        print(f"Failed: {response.error}")
        return 1
    print(response.text)
    if response.input_tokens:
        print(
            f"\n[{response.model} — {response.input_tokens:,} in / "
            f"{response.output_tokens:,} out]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
