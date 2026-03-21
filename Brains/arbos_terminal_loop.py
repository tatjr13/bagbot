#!/usr/bin/env python3
"""Terminal-first Arbos loop runner.

This is a local advisory/operator loop for Brains.
It refreshes operator-facing state, periodically refreshes wallet intel,
and can call Chutes on a configurable cadence to produce loop notes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
ARBOS_DIR = ROOT / "arbos"
CONTROL_ROOT_CANDIDATES = (
    Path("/home/timt/Marvin-Control-Vault/Marvin/Arbos"),
    Path("/root/obsidian-control-vault/Marvin/Arbos"),
)
DEFAULT_LOG_PATH = PROJECT_ROOT / "staking.log"
DEFAULT_GOAL_PATH = ARBOS_DIR / "FALCON_GOAL.md"
DEFAULT_PROMPT_PATH = ARBOS_DIR / "PROMPT.md"
DEFAULT_CHUTES_BASE_URL = "https://llm.chutes.ai/v1/chat/completions"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2-TEE"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Brains.arbos_control_surface import sync_root_views
from Brains.arbos_status import build_status
from Brains.arbos_task_board import promote_queued_task, task_snapshot
from Brains.wallet_tracker import refresh_tracker


def resolve_control_root() -> Path:
    for candidate in CONTROL_ROOT_CANDIDATES:
        if candidate.exists():
            return candidate
    return ARBOS_DIR


CONTROL_ROOT = resolve_control_root()
DEFAULT_STATUS_PATH = CONTROL_ROOT / "STATUS.md"
DEFAULT_WALLET_REPORT_PATH = CONTROL_ROOT / "REPORTS" / "wallet-intel.md"
DEFAULT_TASKS_PATH = CONTROL_ROOT / "TASKS.md"
DEFAULT_RUNS_DIR = CONTROL_ROOT / "RUNS"
DEFAULT_LATEST_RESPONSE = CONTROL_ROOT / "LATEST_RESPONSE.md"
DEFAULT_OUTBOX_PATH = CONTROL_ROOT / "OUTBOX.md"
DEFAULT_LOOP_LOG = CONTROL_ROOT / "LOOP.log"
DEFAULT_WALLET_STATUS_PATH = CONTROL_ROOT / "WALLET_INTEL_STATUS.md"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_text(path: Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fallback


def tail_text(path: Path, line_count: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return f"Missing log: {path}"
    if not lines:
        return "No log lines yet."
    return "\n".join(lines[-line_count:])


def clip(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 16] + "\n...[truncated]\n"


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{iso_now()}] {message}\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_loop_prompt(goal: str, status: str, tasks: str, wallets: str, recent_log: str) -> str:
    return (
        "This is one terminal-first Arbos loop cycle.\n\n"
        "Priority order:\n"
        "1. Respect bankroll safety and read-only/advisory constraints unless explicitly changed elsewhere.\n"
        "2. If a real trade is actionable, say so clearly.\n"
        "3. If no trade is actionable, advance the highest-value unfinished Arbos task.\n"
        "4. Keep learning and keep the queue moving.\n\n"
        "Respond in this exact structure:\n"
        "## Loop Read\n"
        "## Decision\n"
        "## Arbos Task Update\n"
        "## Next Step\n\n"
        "Be concise, factual, and operator-usable.\n\n"
        f"# Goal\n{goal}\n\n"
        f"# Current Status\n{status}\n\n"
        f"# Arbos Tasks\n{tasks}\n\n"
        f"# Wallet Intel\n{wallets}\n\n"
        f"# Recent Log\n{recent_log}\n"
    )


def fallback_response(reason: str, focus_task: str) -> str:
    return (
        "## Loop Read\n"
        f"External reasoning did not complete cleanly: {reason}\n\n"
        "## Decision\n"
        "Keep the loop alive, preserve the current task focus, and retry on the next cadence.\n\n"
        "## Arbos Task Update\n"
        f"Focus task remains `{focus_task}`.\n\n"
        "## Next Step\n"
        "Use the existing status and task board, then retry automatically on the next scheduled cycle.\n"
    )


def call_chutes(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(base_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            data = json.loads(raw)
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"No choices in response: {raw[:500]}")
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(str(item.get("text", "")))
                combined = "\n".join(part for part in text_parts if part).strip()
                if combined:
                    return combined
            raise RuntimeError(f"Empty content in response: {raw[:500]}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2 ** attempt, 15))
    assert last_error is not None
    raise last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the terminal-first Arbos loop.")
    parser.add_argument("--cycle-seconds", type=float, default=15.0)
    parser.add_argument("--status-seconds", type=float, default=30.0)
    parser.add_argument("--wallet-seconds", type=float, default=0.0)
    parser.add_argument("--chutes-seconds", type=float, default=60.0)
    parser.add_argument("--log-lines", type=int, default=40)
    parser.add_argument("--status-tail-lines", type=int, default=4000)
    parser.add_argument("--wallet-timeout", type=float, default=20.0)
    parser.add_argument("--chutes-timeout", type=float, default=90.0)
    parser.add_argument("--chutes-retries", type=int, default=3)
    parser.add_argument("--wallet-force-first", action="store_true")
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--budget-per-day", type=int, default=5000)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_CHUTES_BASE_URL)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=1800)
    parser.add_argument("--api-key-env", default="CHUTES_API_KEY")
    parser.add_argument("--goal", default=str(DEFAULT_GOAL_PATH))
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT_PATH))
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS_PATH))
    parser.add_argument("--status", default=str(DEFAULT_STATUS_PATH))
    parser.add_argument("--wallet-report", default=str(DEFAULT_WALLET_REPORT_PATH))
    parser.add_argument("--outbox", default=str(DEFAULT_OUTBOX_PATH))
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--latest-response", default=str(DEFAULT_LATEST_RESPONSE))
    parser.add_argument("--loop-log", default=str(DEFAULT_LOOP_LOG))
    parser.add_argument("--wallet-status", default=str(DEFAULT_WALLET_STATUS_PATH))
    parser.add_argument("--wallet-config", default=str(ARBOS_DIR / "WALLET_TRACKERS_SEEDS.json"))
    parser.add_argument("--wallet-state", default=str(ARBOS_DIR / "WALLET_TRACKERS_STATE.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    goal_path = Path(args.goal)
    prompt_path = Path(args.prompt)
    tasks_path = Path(args.tasks)
    status_path = Path(args.status)
    wallet_report_path = Path(args.wallet_report)
    outbox_path = Path(args.outbox)
    log_path = Path(args.log_path)
    runs_dir = Path(args.runs_dir)
    latest_response_path = Path(args.latest_response)
    loop_log_path = Path(args.loop_log)
    wallet_status_path = Path(args.wallet_status)
    wallet_config_path = Path(args.wallet_config)
    wallet_state_path = Path(args.wallet_state)

    api_key = os.environ.get(args.api_key_env, "").strip()
    calls_per_day = 86400.0 / max(args.chutes_seconds, 1.0)
    budget_note = (
        f"chutes_calls_per_day≈{calls_per_day:.0f} at interval={args.chutes_seconds:.1f}s "
        f"against budget={args.budget_per_day}"
    )
    startup = f"arbos-loop starting | {budget_note}"
    print(startup, flush=True)
    log_line(loop_log_path, startup)

    if calls_per_day > args.budget_per_day:
        warning = "configured Chutes cadence exceeds stated daily budget"
        print(warning, flush=True)
        log_line(loop_log_path, warning)

    startup_now = time.time()
    last_status = 0.0
    last_wallet = startup_now if not args.wallet_force_first else 0.0
    last_chutes = 0.0
    force_wallet = bool(args.wallet_force_first)

    while True:
        now = time.time()
        try:
            if now - last_status >= max(args.status_seconds, 1.0):
                promoted, focus_title = promote_queued_task(tasks_path, outbox_path=outbox_path)
                status_markdown = build_status(
                    log_path=log_path,
                    wallet_report_path=wallet_report_path,
                    tail_count=int(args.status_tail_lines),
                    tasks_path=tasks_path,
                )
                write_text(status_path, status_markdown)
                sync_root_views(
                    control_root=status_path.parent,
                    tasks_text=read_text(tasks_path, "No Arbos tasks configured."),
                    status_text=status_markdown,
                    wallet_status_text=read_text(wallet_status_path, ""),
                )
                last_status = now
                if promoted and focus_title:
                    log_line(loop_log_path, f"status refreshed | promoted task={focus_title}")
                else:
                    log_line(loop_log_path, "status refreshed")

            if args.wallet_seconds > 0 and now - last_wallet >= args.wallet_seconds:
                refresh_tracker(
                    config_path=wallet_config_path,
                    state_path=wallet_state_path,
                    report_output=wallet_report_path,
                    desktop_output=None,
                    timeout=float(args.wallet_timeout),
                    force=force_wallet,
                )
                force_wallet = False
                status_markdown = build_status(
                    log_path=log_path,
                    wallet_report_path=wallet_report_path,
                    tail_count=int(args.status_tail_lines),
                    tasks_path=tasks_path,
                )
                write_text(status_path, status_markdown)
                sync_root_views(
                    control_root=status_path.parent,
                    tasks_text=read_text(tasks_path, "No Arbos tasks configured."),
                    status_text=status_markdown,
                    wallet_status_text=read_text(wallet_status_path, ""),
                )
                last_status = now
                last_wallet = now
                log_line(loop_log_path, "wallet intel refreshed")

            if now - last_chutes >= max(args.chutes_seconds, 1.0):
                goal = read_text(goal_path, "Goal file missing.")
                system_prompt = clip(read_text(prompt_path, "You are Arbos."), 9000)
                tasks = read_text(tasks_path, "No Arbos tasks configured.")
                status = read_text(status_path, "Status file missing.")
                wallets = clip(read_text(wallet_report_path, "Wallet report missing."), 8000)
                recent_log = clip(tail_text(log_path, int(args.log_lines)), 4000)
                snapshot = task_snapshot(tasks_path)
                log_line(loop_log_path, f"starting chutes cycle | focus={snapshot['focus']}")
                user_prompt = build_loop_prompt(
                    goal,
                    status,
                    tasks,
                    wallets,
                    "Focus task: "
                    f"{snapshot['focus']}\n"
                    f"Active titles: {snapshot['active_titles']}\n"
                    f"Queued titles: {snapshot['queued_titles']}\n\n"
                    f"{recent_log}",
                )
                user_prompt = clip(user_prompt, 12000)

                stamp = utc_now().strftime("%Y%m%d_%H%M%S")
                prompt_out = runs_dir / f"{stamp}_prompt.md"
                write_text(prompt_out, user_prompt)

                if args.dry_run:
                    response = (
                        "## Loop Read\nDry run only.\n\n"
                        "## Decision\nNo Chutes call was made.\n\n"
                        "## Arbos Task Update\nReview prompt file and tune cadence/model before enabling live calls.\n\n"
                        "## Next Step\nRun without --dry-run once CHUTES_API_KEY is available.\n"
                    )
                elif not api_key:
                    response = (
                        "## Loop Read\nCHUTES_API_KEY is not set.\n\n"
                        "## Decision\nSkipping external reasoning cycle.\n\n"
                        "## Arbos Task Update\nLocal status and wallet intel can still refresh.\n\n"
                        "## Next Step\nExport CHUTES_API_KEY and restart the loop.\n"
                    )
                else:
                    try:
                        response = call_chutes(
                            api_key=api_key,
                            model=args.model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            base_url=args.base_url,
                            temperature=float(args.temperature),
                            max_tokens=int(args.max_tokens),
                            timeout=float(args.chutes_timeout),
                            retries=int(args.chutes_retries),
                        )
                    except Exception as exc:  # noqa: BLE001
                        response = fallback_response(str(exc), str(snapshot["focus"]))
                        log_line(loop_log_path, f"chutes cycle fallback | reason={exc}")

                response_out = runs_dir / f"{stamp}_response.md"
                write_text(response_out, response)
                write_text(latest_response_path, response)
                print(f"[{iso_now()}] chutes cycle complete | {response_out.name}", flush=True)
                log_line(loop_log_path, f"chutes cycle complete | run={response_out.name}")
                last_chutes = now

        except KeyboardInterrupt:
            print("arbos-loop stopped by user", flush=True)
            log_line(loop_log_path, "stopped by user")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[{iso_now()}] loop error: {exc}", flush=True)
            traceback.print_exc()
            log_line(loop_log_path, f"loop error: {exc}")

        if args.run_once:
            return 0
        time.sleep(max(args.cycle_seconds, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
