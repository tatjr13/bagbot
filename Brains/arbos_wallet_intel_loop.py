#!/usr/bin/env python3
"""Slow sidecar loop for wallet-intel refreshes."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
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

from Brains.arbos_control_surface import write_text as write_control_text


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
DEFAULT_STATUS_PATH = CONTROL_ROOT / "WALLET_INTEL_STATUS.md"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{iso_now()}] {message}\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_status(path: Path, content: str) -> None:
    write_text(path, content)
    marvin_root = path.parent.parent
    write_control_text(marvin_root / "ARBOS_WALLET_INTEL.md", content)


def run_wallet_tracker(
    *,
    config_path: Path,
    state_path: Path,
    report_path: Path,
    timeout: float,
    force: bool,
) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "wallet_tracker.py"),
        "--config",
        str(config_path),
        "--state",
        str(state_path),
        "--report-output",
        str(report_path),
        "--timeout",
        str(timeout),
        "--no-desktop-output",
    ]
    if force:
        cmd.append("--force")
    hard_timeout = max(timeout + 5.0, 10.0)
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=hard_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"wallet tracker timed out after {hard_timeout:.0f}s") from exc
    if completed.returncode == 0:
        return
    details = (completed.stderr or completed.stdout).strip()
    if details:
        raise RuntimeError(details.splitlines()[-1])
    raise RuntimeError(f"wallet_tracker.py exited with code {completed.returncode}")


def render_status(
    *,
    state: str,
    cycle_seconds: float,
    timeout: float,
    report_path: Path,
    last_started_at: str | None = None,
    last_completed_at: str | None = None,
    last_error: str | None = None,
    next_run_at: str | None = None,
    note: str | None = None,
) -> str:
    lines = [
        "# Wallet Intel Status",
        "",
        f"- State: {state}",
        f"- Cycle seconds: {int(cycle_seconds)}",
        f"- Timeout seconds: {int(timeout)}",
        f"- Report path: `{report_path}`",
    ]
    if last_started_at:
        lines.append(f"- Last started: {last_started_at}")
    if last_completed_at:
        lines.append(f"- Last completed: {last_completed_at}")
    if next_run_at:
        lines.append(f"- Next planned run: {next_run_at}")
    if last_error:
        lines.append(f"- Last error: {last_error}")
    if note:
        lines.append(f"- Note: {note}")
    lines.append("")
    return "\n".join(lines)


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
    parser.add_argument("--status", default=str(DEFAULT_STATUS_PATH))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    state_path = Path(args.state)
    report_path = Path(args.report)
    log_path = Path(args.log)
    status_path = Path(args.status)

    force_next = bool(args.force_first)
    startup = f"arbos-wallet-intel starting | cycle={args.cycle_seconds:.0f}s timeout={args.timeout:.0f}s"
    print(startup, flush=True)
    log_line(log_path, startup)
    write_status(
        status_path,
        render_status(
            state="starting",
            cycle_seconds=float(args.cycle_seconds),
            timeout=float(args.timeout),
            report_path=report_path,
            note="wallet-intel sidecar booting",
        ),
    )

    while True:
        try:
            started_at = iso_now()
            write_status(
                status_path,
                render_status(
                    state="refreshing",
                    cycle_seconds=float(args.cycle_seconds),
                    timeout=float(args.timeout),
                    report_path=report_path,
                    last_started_at=started_at,
                    note="refreshing Taostats wallet intel",
                ),
            )
            log_line(log_path, "wallet tracker refresh starting")
            run_wallet_tracker(
                config_path=config_path,
                state_path=state_path,
                report_path=report_path,
                timeout=float(args.timeout),
                force=force_next,
            )
            force_next = False
            log_line(log_path, "wallet tracker refresh complete")
            completed_at = iso_now()
            next_run_at = datetime.now(timezone.utc).replace(microsecond=0).astimezone(timezone.utc)
            next_run_at = (next_run_at.timestamp() + max(args.cycle_seconds, 30.0))
            write_status(
                status_path,
                render_status(
                    state="idle",
                    cycle_seconds=float(args.cycle_seconds),
                    timeout=float(args.timeout),
                    report_path=report_path,
                    last_started_at=started_at,
                    last_completed_at=completed_at,
                    next_run_at=datetime.fromtimestamp(next_run_at, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    note="waiting for next scheduled wallet-intel refresh",
                ),
            )
        except KeyboardInterrupt:
            log_line(log_path, "stopped by user")
            write_status(
                status_path,
                render_status(
                    state="stopped",
                    cycle_seconds=float(args.cycle_seconds),
                    timeout=float(args.timeout),
                    report_path=report_path,
                    note="wallet-intel sidecar stopped by user",
                ),
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            log_line(log_path, f"wallet tracker refresh failed: {exc}")
            write_status(
                status_path,
                render_status(
                    state="error",
                    cycle_seconds=float(args.cycle_seconds),
                    timeout=float(args.timeout),
                    report_path=report_path,
                    last_error=str(exc),
                    note="wallet-intel refresh failed; will retry on next cycle",
                ),
            )
            print(f"[{iso_now()}] wallet-intel loop error: {exc}", file=sys.stderr, flush=True)

        if args.run_once:
            return 0
        time.sleep(max(args.cycle_seconds, 30.0))


if __name__ == "__main__":
    raise SystemExit(main())
