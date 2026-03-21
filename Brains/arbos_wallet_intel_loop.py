#!/usr/bin/env python3
"""Slow sidecar loop for wallet-intel refreshes."""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
CONTROL_ROOT_CANDIDATES = (
    Path("/home/timt/Marvin-Control-Vault/Marvin/Arbos"),
    Path("/root/obsidian-control-vault/Marvin/Arbos"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Brains.wallet_tracker import refresh_tracker


def resolve_control_root() -> Path:
    for candidate in CONTROL_ROOT_CANDIDATES:
        if candidate.exists():
            return candidate
    return ROOT / "arbos"


CONTROL_ROOT = resolve_control_root()
DEFAULT_CONFIG_PATH = ROOT / "arbos" / "WALLET_TRACKERS_SEEDS.json"
DEFAULT_STATE_PATH = ROOT / "arbos" / "WALLET_TRACKERS_STATE.json"
DEFAULT_REPORT_PATH = CONTROL_ROOT / "REPORTS" / "wallet-intel.md"
DEFAULT_LOG_PATH = CONTROL_ROOT / "WALLET_INTEL.log"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{iso_now()}] {message}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Arbos wallet-intel sidecar loop.")
    parser.add_argument("--cycle-seconds", type=float, default=300.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--force-first", action="store_true")
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    state_path = Path(args.state)
    report_path = Path(args.report)
    log_path = Path(args.log)

    force_next = bool(args.force_first)
    startup = f"arbos-wallet-intel starting | cycle={args.cycle_seconds:.0f}s timeout={args.timeout:.0f}s"
    print(startup, flush=True)
    log_line(log_path, startup)

    while True:
        try:
            refresh_tracker(
                config_path=config_path,
                state_path=state_path,
                report_output=report_path,
                desktop_output=None,
                timeout=float(args.timeout),
                force=force_next,
            )
            force_next = False
            log_line(log_path, "wallet tracker refresh complete")
        except KeyboardInterrupt:
            log_line(log_path, "stopped by user")
            return 0
        except Exception as exc:  # noqa: BLE001
            log_line(log_path, f"wallet tracker refresh failed: {exc}")
            traceback.print_exc()

        if args.run_once:
            return 0
        time.sleep(max(args.cycle_seconds, 30.0))


if __name__ == "__main__":
    raise SystemExit(main())
