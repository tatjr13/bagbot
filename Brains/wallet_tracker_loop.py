"""Periodic wallet-intel and status refresh loop for Bagbot/Arbos."""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "arbos" / "WALLET_TRACKERS_SEEDS.json"
DEFAULT_STATE_PATH = ROOT / "arbos" / "WALLET_TRACKERS_STATE.json"
DEFAULT_REPORT_PATH = ROOT / "arbos" / "WALLET_TRACKERS.md"
DEFAULT_STATUS_OUTPUT = ROOT / "arbos" / "ARBOS_STATUS.md"
DEFAULT_LOG_PATH = ROOT.parent / "staking.log"

if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from Brains.arbos_status import build_status
from Brains.wallet_tracker import refresh_tracker


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unique_paths(paths: Iterable[Path]) -> List[Path]:
    ordered: List[Path] = []
    seen = set()
    for path in paths:
        resolved = path.expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(resolved)
    return ordered


def _mirror_text(source_path: Path, targets: Iterable[Path]) -> None:
    content = source_path.read_text(encoding="utf-8")
    for target in _unique_paths(targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def run_cycle(
    config_path: Path,
    state_path: Path,
    report_output: Path,
    report_mirrors: Iterable[Path],
    status_output: Path,
    status_mirrors: Iterable[Path],
    log_path: Path,
    timeout: float,
    tail_lines: int,
    force: bool,
) -> dict:
    state = refresh_tracker(
        config_path=config_path,
        state_path=state_path,
        report_output=report_output,
        desktop_output=None,
        timeout=timeout,
        force=force,
    )
    _mirror_text(report_output, report_mirrors)

    status_markdown = build_status(
        log_path=log_path,
        wallet_report_path=report_output,
        tail_count=tail_lines,
    )
    status_output.parent.mkdir(parents=True, exist_ok=True)
    status_output.write_text(status_markdown, encoding="utf-8")
    for mirror in _unique_paths(status_mirrors):
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(status_markdown, encoding="utf-8")
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Bagbot wallet-intel sidecar loop.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--report-output", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--report-mirror", action="append", default=[])
    parser.add_argument("--status-output", default=str(DEFAULT_STATUS_OUTPUT))
    parser.add_argument("--status-mirror", action="append", default=[])
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--tail-lines", type=int, default=4000)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--force-first-refresh", action="store_true")
    parser.add_argument("--run-once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    state_path = Path(args.state)
    report_output = Path(args.report_output)
    report_mirrors = [Path(value) for value in args.report_mirror]
    status_output = Path(args.status_output)
    status_mirrors = [Path(value) for value in args.status_mirror]
    log_path = Path(args.log_path)

    force = bool(args.force_first_refresh)
    while True:
        try:
            state = run_cycle(
                config_path=config_path,
                state_path=state_path,
                report_output=report_output,
                report_mirrors=report_mirrors,
                status_output=status_output,
                status_mirrors=status_mirrors,
                log_path=log_path,
                timeout=float(args.timeout),
                tail_lines=int(args.tail_lines),
                force=force,
            )
            meta = state.get("meta", {})
            print(
                f"[{_utc_now()}] wallet-intel refreshed | "
                f"tracked={meta.get('tracked_wallet_count', 0)} "
                f"candidates={meta.get('candidate_count', 0)} "
                f"promoted={meta.get('promoted_count', 0)} "
                f"cursor={meta.get('wallet_cursor', 0)}",
                flush=True,
            )
        except Exception:
            print(f"[{_utc_now()}] wallet-intel cycle failed", flush=True)
            traceback.print_exc()
        if args.run_once:
            return 0
        force = False
        time.sleep(max(float(args.interval_seconds), 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
