"""Minimal Trading 212 Public API (v0) client.

Docs: https://docs.trading212.com/api

Auth is HTTP Basic: API key as username, API secret as password.
Older single-key credentials are still accepted via the legacy
``Authorization: <key>`` header — pass only ``api_key`` to use that path.

Rate limits are per-account, not per-key. The endpoints used here:
    GET /equity/account/summary   1 req / 5s
    GET /equity/positions         1 req / 1s
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

import requests

LIVE = "https://live.trading212.com/api/v0"
DEMO = "https://demo.trading212.com/api/v0"

# Where we look for the credentials file, in order. Set T212_ENV_FILE to
# override with an explicit path.
ENV_CANDIDATES = (".env", "keys/.env", "config/.env", "secrets/.env")


class T212Error(RuntimeError):
    """Raised when the API returns something we cannot use."""


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Existing environment variables win, so you can override the file
    from the shell. Values may be quoted; ``#`` starts a comment only
    at the beginning of a line.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def find_env_file(root: Path) -> Path | None:
    """Locate the credentials file, checking the usual spots."""
    override = os.environ.get("T212_ENV_FILE", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None
    for relative in ENV_CANDIDATES:
        candidate = root / relative
        if candidate.exists():
            return candidate
    return None


class Trading212:
    def __init__(
        self,
        api_key: str,
        api_secret: str | None = None,
        environment: str = "live",
        timeout: int = 30,
    ) -> None:
        if not api_key:
            raise T212Error(
                "No API key found. Put T212_API_KEY and T212_API_SECRET in a "
                ".env file at one of: " + ", ".join(ENV_CANDIDATES) + " "
                "(relative to the project root), or set T212_ENV_FILE to its "
                "full path."
            )
        if api_key.startswith("your_") or (api_secret or "").startswith("your_"):
            raise T212Error(
                "Your .env still has the placeholder values from .env.example. "
                "Replace them with the real key and secret from the Trading 212 "
                "app (Settings -> API)."
            )
        self.base = DEMO if environment.lower() in {"demo", "practice"} else LIVE
        self.timeout = timeout
        self.session = requests.Session()

        if api_secret:
            token = base64.b64encode(
                f"{api_key}:{api_secret}".encode("utf-8")
            ).decode("ascii")
            auth = f"Basic {token}"
        else:
            # Legacy single-key header.
            auth = api_key

        self.session.headers.update(
            {"Authorization": auth, "Accept": "application/json"}
        )

    @classmethod
    def from_env(
        cls, env_file: Path | None = None, root: Path | None = None
    ) -> "Trading212":
        """Build a client from a .env file.

        Pass ``env_file`` for an explicit path, or ``root`` to search the
        usual locations beneath the project directory.
        """
        if env_file is None and root is not None:
            env_file = find_env_file(root)
        if env_file is not None:
            load_dotenv(env_file)
        return cls(
            api_key=os.environ.get("T212_API_KEY", "").strip(),
            api_secret=os.environ.get("T212_API_SECRET", "").strip() or None,
            environment=os.environ.get("T212_ENV", "live").strip(),
        )

    # ---------------------------------------------------------------- http

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with retry on 429 and transient 5xx.

        On 429 the API tells us when the window resets via
        ``x-ratelimit-reset`` (unix seconds); we wait for that rather
        than guessing.
        """
        url = f"{self.base}{path}"
        delay = 2.0

        for attempt in range(5):
            response = self.session.get(url, params=params, timeout=self.timeout)

            if response.status_code == 200:
                return response.json()

            if response.status_code == 401:
                raise T212Error(
                    "401 Unauthorized — check T212_API_KEY / T212_API_SECRET, "
                    "and that the key belongs to the environment you set "
                    f"(T212_ENV={'demo' if self.base == DEMO else 'live'})."
                )
            if response.status_code == 403:
                raise T212Error(
                    "403 Forbidden — the key may lack the required scope, or "
                    "an IP restriction on the key is blocking this machine."
                )
            if response.status_code == 429:
                reset = response.headers.get("x-ratelimit-reset")
                wait = delay
                if reset:
                    try:
                        wait = max(1.0, float(reset) - time.time()) + 0.5
                    except ValueError:
                        pass
                time.sleep(min(wait, 90))
                delay *= 2
                continue
            if 500 <= response.status_code < 600:
                time.sleep(delay)
                delay *= 2
                continue

            raise T212Error(
                f"{response.status_code} from {path}: {response.text[:300]}"
            )

        raise T212Error(f"Gave up on {path} after repeated rate limiting.")

    # ------------------------------------------------------------ endpoints

    def account_summary(self) -> dict[str, Any]:
        """Cash, invested capital, realised/unrealised P&L, total value."""
        return self._get("/equity/account/summary")

    def positions(self) -> list[dict[str, Any]]:
        """All open positions with instrument detail and wallet impact."""
        data = self._get("/equity/positions")
        if isinstance(data, dict):  # tolerate a paginated shape
            data = data.get("items", [])
        return list(data or [])

    def dividends(self, limit: int = 50, max_pages: int = 20) -> list[dict]:
        """Paid-out dividends, newest first (cursor paginated)."""
        return self._paginate("/equity/history/dividends", limit, max_pages)

    def transactions(self, limit: int = 50, max_pages: int = 20) -> list[dict]:
        """Cash movements in and out of the account."""
        return self._paginate("/equity/history/transactions", limit, max_pages)

    def _paginate(self, path: str, limit: int, max_pages: int) -> list[dict]:
        items: list[dict] = []
        params: dict[str, Any] | None = {"limit": limit}
        next_path = path

        for _ in range(max_pages):
            payload = self._get(next_path, params)
            params = None  # nextPagePath already carries the query string
            if isinstance(payload, list):
                items.extend(payload)
                break
            items.extend(payload.get("items", []))
            following = payload.get("nextPagePath")
            if not following:
                break
            next_path = following.replace("/api/v0", "", 1)
            time.sleep(10)  # history endpoints allow 6 req / minute

        return items
