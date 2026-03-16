"""Minimal read-only Taostats API helper with built-in rate limiting.

Usage examples:
  python Brains/taostats_api.py /api/stats/latest/v1
  python Brains/taostats_api.py /api/subnets/v1 --param page=1 --param limit=50

Authentication:
  Export TAOSTATS_API_KEY in the environment before use.

Rate limiting:
  Default is 5 requests per minute. The helper enforces a 12 second gap
  between requests across processes using a lock file in the system temp dir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import fcntl


BASE_URL = "https://api.taostats.io"
DEFAULT_RATE_LIMIT_PER_MIN = 5
DEFAULT_TIMEOUT_SECONDS = 20.0
RATE_LIMIT_FILE = Path(tempfile.gettempdir()) / "taostats_rate_limit.json"
RATE_LIMIT_LOCK = Path(tempfile.gettempdir()) / "taostats_rate_limit.lock"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query the Taostats API safely.")
    parser.add_argument("path", help="API path such as /api/stats/latest/v1")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Repeatable querystring parameter.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--rate-limit-per-minute",
        type=float,
        default=float(os.environ.get("TAOSTATS_RATE_LIMIT_PER_MIN", DEFAULT_RATE_LIMIT_PER_MIN)),
        help="Maximum Taostats requests per minute. Default: 5.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Fail instead of sleeping when the rate limit window has not cleared.",
    )
    return parser.parse_args()


def build_url(path: str, raw_params: list[str]) -> str:
    if not path.startswith("/"):
        path = f"/{path}"

    params = []
    for item in raw_params:
        if "=" not in item:
            raise ValueError(f"Invalid --param value '{item}'. Use KEY=VALUE.")
        key, value = item.split("=", 1)
        params.append((key, value))

    query = urllib.parse.urlencode(params)
    return f"{BASE_URL}{path}" + (f"?{query}" if query else "")


def enforce_rate_limit(rate_limit_per_minute: float, no_wait: bool) -> None:
    if rate_limit_per_minute <= 0:
        return

    min_interval = 60.0 / rate_limit_per_minute
    RATE_LIMIT_LOCK.touch(exist_ok=True)

    with RATE_LIMIT_LOCK.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        last_request_at = 0.0
        if RATE_LIMIT_FILE.exists():
            try:
                state = json.loads(RATE_LIMIT_FILE.read_text(encoding="utf-8"))
                last_request_at = float(state.get("last_request_at", 0.0))
            except (json.JSONDecodeError, OSError, ValueError):
                last_request_at = 0.0

        now = time.time()
        wait_seconds = max(0.0, min_interval - (now - last_request_at))
        if wait_seconds > 0:
            if no_wait:
                raise RuntimeError(
                    f"Taostats rate limit not ready for {wait_seconds:.1f}s; retry later."
                )
            time.sleep(wait_seconds)

        RATE_LIMIT_FILE.write_text(
            json.dumps({"last_request_at": time.time()}),
            encoding="utf-8",
        )


def fetch(url: str, timeout: float) -> dict | list:
    api_key = os.environ.get("TAOSTATS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAOSTATS_API_KEY is not set.")

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": api_key,
            "Accept": "application/json",
            "User-Agent": "bagbot-arbos/1.0",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset)
        if not payload.strip():
            return {}
        return json.loads(payload)


def main() -> int:
    args = parse_args()

    try:
        url = build_url(args.path, args.param)
        enforce_rate_limit(args.rate_limit_per_minute, args.no_wait)
        response = fetch(url, args.timeout)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
