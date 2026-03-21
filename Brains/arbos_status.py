"""Generate a concise operator-facing Arbos status report."""

from __future__ import annotations

import argparse
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from Brains.arbos_task_board import task_snapshot


ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = ROOT.parent / "staking.log"
DEFAULT_WALLET_REPORT_PATH = ROOT / "arbos" / "WALLET_TRACKERS.md"
DEFAULT_OUTPUT_PATH = ROOT / "arbos" / "ARBOS_STATUS.md"
DEFAULT_TASKS_PATH = ROOT / "arbos" / "ARBOS_TASKS.md"

WALLET_VALUE_RE = re.compile(r'\{wallet_value:"(?P<staked>[0-9.]+) \+ (?P<liquid>[0-9.]+)", (?P<body>.*)\}')
ROSTER_RE = re.compile(
    r"Brains runtime roster refreshed: live=\[(?P<live>[^\]]*)\], buy_enabled=\[(?P<buy>[^\]]*)\], exit_only=\[(?P<exit>[^\]]*)\]"
)
TIMESTAMP_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an Arbos status markdown file.")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--wallet-report", default=str(DEFAULT_WALLET_REPORT_PATH))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS_PATH))
    parser.add_argument("--tail-lines", type=int, default=4000)
    return parser.parse_args()


def tail_lines(path: Path, max_lines: int) -> List[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque(handle, maxlen=max_lines))


def _last_matching(lines: List[str], needles: tuple[str, ...]) -> Optional[str]:
    for line in reversed(lines):
        if any(needle in line for needle in needles):
            return line.strip()
    return None


def _parse_wallet_snapshot(line: Optional[str]) -> str:
    if not line:
        return "unknown"
    match = WALLET_VALUE_RE.search(line)
    if not match:
        return line
    body = match.group("body")
    holdings = []
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk.startswith("sn"):
            continue
        holdings.append(chunk)
    return f"staked={match.group('staked')} TAO | liquid={match.group('liquid')} TAO | " + ", ".join(holdings)


def _parse_roster(line: Optional[str], group: str) -> str:
    if not line:
        return "unknown"
    match = ROSTER_RE.search(line)
    if not match:
        return "unknown"
    value = match.group(group).strip()
    return value or "none"


def _extract_timestamp(line: Optional[str]) -> str:
    if not line:
        return "unknown"
    match = TIMESTAMP_RE.search(line)
    return match.group("ts") if match else "unknown"


def _wallet_intel_top(wallet_report_path: Path, limit: int = 3) -> str:
    if not wallet_report_path.exists():
        return "wallet tracker report missing"
    lines = wallet_report_path.read_text(encoding="utf-8").splitlines()
    collected = []
    in_candidates = False
    for line in lines:
        if line.strip() == "## Ranked Precursor Candidates":
            in_candidates = True
            continue
        if in_candidates and line.startswith("## "):
            break
        if in_candidates and re.match(r"^\d+\.\s+`", line):
            collected.append(line.strip())
            if len(collected) >= limit:
                break
    return " | ".join(collected) if collected else "no ranked precursor wallets yet"


def _wallet_intel_meta(wallet_report_path: Path) -> str:
    if not wallet_report_path.exists():
        return "wallet tracker report missing"
    lines = wallet_report_path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.startswith("Generated:"):
            return line.replace("Generated:", "").strip()
    return "unknown"


def _wallet_promotion_summary(wallet_report_path: Path) -> str:
    if not wallet_report_path.exists():
        return "wallet tracker report missing"
    lines = wallet_report_path.read_text(encoding="utf-8").splitlines()
    in_active = False
    derived_count = 0
    for line in lines:
        if line.strip() == "## Active Watchlist":
            in_active = True
            continue
        if in_active and line.startswith("## "):
            break
        if in_active and "| tier=`derived`" in line:
            derived_count += 1
    if derived_count == 0:
        return "wallet-intel precursor ranking is live; no derived wallet is currently promoted into the active watchlist."
    if derived_count == 1:
        return "wallet-intel precursor ranking is live; 1 derived wallet is currently promoted into the active watchlist."
    return f"wallet-intel precursor ranking is live; {derived_count} derived wallets are currently promoted into the active watchlist."


def _wallet_recent_moves(wallet_report_path: Path, limit: int = 3) -> str:
    if not wallet_report_path.exists():
        return "wallet tracker report missing"
    lines = wallet_report_path.read_text(encoding="utf-8").splitlines()
    collected = []
    in_moves = False
    for line in lines:
        if line.strip() == "## Recent Movement Ledger":
            in_moves = True
            continue
        if in_moves and line.startswith("## "):
            break
        if in_moves and line.startswith("- "):
            collected.append(line[2:].strip())
            if len(collected) >= limit:
                break
    return " | ".join(collected) if collected else "no tracked wallet movements recorded yet"


def _wallet_status_fields(wallet_report_path: Path) -> dict[str, str]:
    status_path = wallet_report_path.parent.parent / "WALLET_INTEL_STATUS.md"
    if not status_path.exists():
        return {}
    fields: dict[str, str] = {}
    for line in status_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        key, _, value = line[2:].partition(":")
        if not _:
            continue
        fields[key.strip().lower()] = value.strip()
    return fields


def _wallet_sidecar_summary(wallet_report_path: Path) -> str:
    fields = _wallet_status_fields(wallet_report_path)
    if not fields:
        return "wallet-intel sidecar status missing"
    parts = [fields.get("state", "unknown")]
    if fields.get("last completed"):
        parts.append(f"last completed {fields['last completed']}")
    elif fields.get("last started"):
        parts.append(f"started {fields['last started']}")
    if fields.get("next planned run"):
        parts.append(f"next {fields['next planned run']}")
    if fields.get("last error"):
        parts.append(f"error {fields['last error']}")
    return " | ".join(parts)


def build_status(log_path: Path, wallet_report_path: Path, tail_count: int, tasks_path: Path | None = None) -> str:
    lines = tail_lines(log_path, tail_count)
    snapshot_line = _last_matching(lines, ('{wallet_value:"',))
    roster_line = _last_matching(lines, ("Brains runtime roster refreshed:",))
    last_trade_line = _last_matching(
        lines,
        (
            "Staked ",
            "Unstaked ",
            "Rotation swap executed",
            "Failed rotation swap",
            "Attempting atomic swap",
        ),
    )
    last_blocker_line = _last_matching(
        lines,
        (
            "Fee buffer reached",
            "Skipping rotation:",
            "Failed rotation swap",
        ),
    )

    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    holdings = _parse_wallet_snapshot(snapshot_line)
    live_roster = _parse_roster(roster_line, "live")
    buy_enabled = _parse_roster(roster_line, "buy")
    exit_only = _parse_roster(roster_line, "exit")
    last_action = f"{_extract_timestamp(last_trade_line)} | {last_trade_line}" if last_trade_line else "none found"
    last_blocker = f"{_extract_timestamp(last_blocker_line)} | {last_blocker_line}" if last_blocker_line else "none found"
    wallet_intel = _wallet_intel_top(wallet_report_path)
    wallet_meta = _wallet_intel_meta(wallet_report_path)
    wallet_sidecar = _wallet_sidecar_summary(wallet_report_path)
    promotion_summary = _wallet_promotion_summary(wallet_report_path)
    recent_moves = _wallet_recent_moves(wallet_report_path)
    snapshot = task_snapshot(tasks_path) if tasks_path and tasks_path.exists() else {
        "focus": "none",
        "active_titles": [],
        "queued_titles": [],
        "active_count": 0,
        "queued_count": 0,
    }

    lines_out = [
        "# Arbos Status",
        "",
        f"- Updated at: {updated_at}",
        f"- Focus Arbos task: {snapshot['focus']}",
        f"- Active task board: {snapshot['active_count']} active | {snapshot['active_titles'] or ['none']}",
        f"- Queued task board: {snapshot['queued_count']} queued | {snapshot['queued_titles'] or ['none']}",
        f"- Current holdings: {holdings}",
        f"- Current live roster: {live_roster}",
        f"- Buy-enabled: {buy_enabled}",
        f"- Exit-only: {exit_only}",
        f"- Last fill or last blocked action: {last_action}",
        f"- Main reason for no trade, if idle: {last_blocker}",
        f"- Top subnet challengers: current roster watchlist is [{buy_enabled}]",
        f"- Top wallet-intel challengers: {wallet_intel}",
        f"- Wallet-intel sidecar: {wallet_sidecar}",
        f"- Recent tracked wallet moves: {recent_moves}",
        f"- Wallet-intel report generated: {wallet_meta}",
        f"- Current champion vs challenger work: {promotion_summary}",
        "- Next check or experiment: refresh wallet tracker on the next hourly cycle, compare precursor wallets against TAO-flow leaders, and keep watching for fee-buffer or rotation unlocks.",
    ]
    return "\n".join(lines_out) + "\n"


def main() -> int:
    args = parse_args()
    content = build_status(
        log_path=Path(args.log_path),
        wallet_report_path=Path(args.wallet_report),
        tail_count=args.tail_lines,
        tasks_path=Path(args.tasks),
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
