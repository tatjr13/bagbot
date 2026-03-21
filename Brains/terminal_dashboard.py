#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


CONTROL_ROOT_CANDIDATES = (
    Path("/home/timt/Marvin-Control-Vault/Marvin/Arbos"),
    Path("/root/obsidian-control-vault/Marvin/Arbos"),
)


def resolve_control_root(default_root: Path) -> Path:
    for candidate in CONTROL_ROOT_CANDIDATES:
        if candidate.exists():
            return candidate
    return default_root / "arbos"


def read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return f"Missing: {path}"


def read_tasks(path: Path) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return []
    tasks = data.get("tasks", [])
    return tasks if isinstance(tasks, list) else []


def tail_lines(path: Path, count: int = 20) -> str:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return f"Missing: {path}"
    return "\n".join(lines[-count:]) if lines else "No log lines yet."


def build_task_table(tasks: list[dict[str, Any]]) -> Table:
    table = Table(expand=True)
    table.add_column("Task")
    table.add_column("Status", width=12)
    table.add_column("Loop", width=10)
    table.add_column("Note")
    for task in tasks:
        table.add_row(
            str(task.get("name", "unnamed")),
            str(task.get("status", "unknown")),
            str(task.get("cadence", "-")),
            str(task.get("note", "")),
        )
    if not tasks:
        table.add_row("No tasks configured", "-", "-", "Add tasks to TERMINAL_AGENT_TASKS.yaml")
    return table


def build_layout(args: argparse.Namespace) -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=10),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )
    layout["left"].split_column(
        Layout(name="tasks", ratio=1),
        Layout(name="goal", ratio=2),
    )
    layout["right"].split_column(
        Layout(name="status", ratio=2),
        Layout(name="wallets", ratio=3),
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text(f"Arbos Terminal Dashboard  |  {now}  |  refresh={args.refresh}s", style="bold cyan")
    layout["header"].update(Panel(header_text))

    task_path = Path(args.tasks)
    if task_path.suffix.lower() == ".yaml":
        tasks = read_tasks(task_path)
        task_panel = Panel(build_task_table(tasks), title="Task Board")
    else:
        task_panel = Panel(Markdown(read_text(task_path)), title="Task Board")

    layout["tasks"].update(task_panel)
    layout["goal"].update(Panel(Markdown(read_text(Path(args.goal))), title="Goal"))
    layout["status"].update(Panel(Markdown(read_text(Path(args.status))), title="Status"))
    layout["wallets"].update(Panel(Markdown(read_text(Path(args.wallets))), title="Wallet Intel"))

    footer_group = Group(
        Text(f"Log: {args.log}", style="bold"),
        Text(tail_lines(Path(args.log), args.log_lines) or "No log lines yet.")
    )
    layout["footer"].update(Panel(footer_group, title="Recent Log"))
    return layout


def parse_args() -> argparse.Namespace:
    default_root = Path(__file__).resolve().parent
    control_root = resolve_control_root(default_root)
    parser = argparse.ArgumentParser(description="Terminal-first dashboard for Arbos/Bagbot status")
    parser.add_argument("--refresh", type=int, default=5)
    parser.add_argument("--tasks", default=str(control_root / "TASKS.md"))
    parser.add_argument("--goal", default=str(default_root / "arbos" / "FALCON_GOAL.md"))
    parser.add_argument("--status", default=str(control_root / "STATUS.md"))
    parser.add_argument("--wallets", default=str(control_root / "REPORTS" / "wallet-intel.md"))
    parser.add_argument("--log", default=str(control_root / "LOOP.log"))
    parser.add_argument("--log-lines", type=int, default=20)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    with Live(build_layout(args), refresh_per_second=4, screen=True) as live:
        while True:
            live.update(build_layout(args))
            time.sleep(args.refresh)
