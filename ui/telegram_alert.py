"""Telegram sender.

Credentials live in ``keys/.env`` (already gitignored):

    TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
    TELEGRAM_CHAT_ID=987654321

To get them: message @BotFather to create a bot and copy the token, then
message your new bot once and open
``https://api.telegram.org/bot<TOKEN>/getUpdates`` to read your chat id.

    from telegram_alert import send
    send("Morning brief\\n\\nBook is flat.")

Never raises — returns a result dict — because a failed notification
should not take down the scheduled job that produced it.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_CANDIDATES = (ROOT / "keys" / ".env", ROOT / ".env")

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4096          # Telegram's hard limit per message
CHUNK_TARGET = 3800     # leave headroom for the continuation marker

_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def load_env() -> None:
    """Load KEY=VALUE pairs from every .env we know about. Shell wins."""
    for path in ENV_CANDIDATES:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def credentials() -> tuple[str, str]:
    load_env()
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    )


def is_configured() -> bool:
    token, chat_id = credentials()
    return bool(token and chat_id and not token.startswith("your_"))


def escape_markdown_v2(text: str) -> str:
    """Escape the characters MarkdownV2 treats as syntax."""
    return re.sub(f"([{re.escape(_MDV2_SPECIALS)}])", r"\\\1", text)


def _chunk(text: str, size: int = CHUNK_TARGET) -> list[str]:
    """Split on paragraph boundaries where possible, never mid-word."""
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        cut = window.rfind("\n\n")
        if cut < size // 2:
            cut = window.rfind("\n")
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = size
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send(
    text: str,
    *,
    parse_mode: str | None = None,
    disable_preview: bool = True,
    retries: int = 3,
) -> dict:
    """Send a message, splitting it if Telegram's limit demands.

    ``parse_mode`` of ``"MarkdownV2"`` escapes the body automatically.
    Returns ``{"ok": bool, "sent": int, "error": str}``.
    """
    token, chat_id = credentials()
    if not token or not chat_id:
        return {
            "ok": False,
            "sent": 0,
            "error": (
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing. Add them to keys/.env"
            ),
        }

    try:
        import requests
    except ImportError:
        return {"ok": False, "sent": 0, "error": "requests not installed"}

    body = escape_markdown_v2(text) if parse_mode == "MarkdownV2" else text
    parts = _chunk(body)
    sent = 0

    for index, part in enumerate(parts):
        if len(parts) > 1:
            marker = f"({index + 1}/{len(parts)})\n\n"
            part = (escape_markdown_v2(marker) if parse_mode == "MarkdownV2" else marker) + part

        payload = {
            "chat_id": chat_id,
            "text": part[:MAX_LEN],
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        delay = 2.0
        for attempt in range(retries):
            try:
                response = requests.post(
                    API.format(token=token), json=payload, timeout=25
                )
                if response.status_code == 200:
                    sent += 1
                    break

                if response.status_code == 429:
                    wait = response.json().get("parameters", {}).get("retry_after", delay)
                    time.sleep(min(float(wait) + 0.5, 60))
                    delay *= 2
                    continue

                if response.status_code == 400 and parse_mode:
                    # Almost always a markdown parse failure. Resend as plain
                    # text rather than losing the message entirely.
                    payload.pop("parse_mode", None)
                    payload["text"] = text[:MAX_LEN]
                    parse_mode = None
                    continue

                return {
                    "ok": False,
                    "sent": sent,
                    "error": f"{response.status_code}: {response.text[:200]}",
                }

            except Exception as error:  # noqa: BLE001
                if attempt == retries - 1:
                    return {"ok": False, "sent": sent, "error": str(error)[:200]}
                time.sleep(delay)
                delay *= 2

        time.sleep(0.4)  # stay under Telegram's per-chat rate limit

    return {"ok": sent == len(parts), "sent": sent, "error": ""}


def send_test() -> dict:
    """Round-trip check that the bot token and chat id work."""
    return send(
        "Maple connected. This is a test message — "
        "your daily brief will arrive here."
    )


if __name__ == "__main__":
    if not is_configured():
        print("Not configured. Add to keys/.env:")
        print("  TELEGRAM_BOT_TOKEN=...")
        print("  TELEGRAM_CHAT_ID=...")
        raise SystemExit(1)
    print(send_test())
