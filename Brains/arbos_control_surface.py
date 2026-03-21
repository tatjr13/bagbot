"""Helpers for keeping the Marvin/Arbos control surface fresh."""

from __future__ import annotations

import re
from pathlib import Path


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def extract_titles_from_section(tasks_text: str, section: str) -> list[str]:
    match = re.search(rf"(?ms)^## {re.escape(section)}\s*(.*?)^(## |\Z)", tasks_text)
    if not match:
        return []
    return [m.group(1).strip() for m in re.finditer(r"(?m)^### ([^\n]+)$", match.group(1))]


def bullet_lines(text: str, limit: int) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")][:limit]


def sync_root_views(
    *,
    control_root: Path,
    tasks_text: str,
    status_text: str,
    wallet_status_text: str = "",
) -> None:
    marvin_root = control_root.parent
    write_text(marvin_root / "ARBOS_TASKS.md", tasks_text)
    write_text(marvin_root / "ARBOS_STATUS.md", status_text)
    if wallet_status_text.strip():
        write_text(marvin_root / "ARBOS_WALLET_INTEL.md", wallet_status_text)

    active_titles = extract_titles_from_section(tasks_text, "Active")
    queued_titles = extract_titles_from_section(tasks_text, "Queued")
    status_lines = bullet_lines(status_text, 7)
    wallet_lines = bullet_lines(wallet_status_text, 5)

    overview = ["# Arbos Bot", ""]
    overview.append("Arbos Bot is the continuous worker loop.")
    overview.append("Marvin is the manager, injector, and relay.")
    overview.append("")
    overview.append("## Main Files")
    overview.append("- `Arbos/TASKS.md` — canonical task board")
    overview.append("- `Arbos/STATUS.md` — live loop state")
    overview.append("- `Arbos/WALLET_INTEL_STATUS.md` — wallet-intel sidecar state")
    overview.append("- `Arbos/OUTBOX.md` — short relay items for Tim")
    overview.append("- `Arbos/REPORTS/` — detailed outputs")
    overview.append("")
    overview.append("## Current Active Tasks")
    if active_titles:
        overview.extend([f"- `{title}`" for title in active_titles])
    else:
        overview.append("- none")
    overview.append("")
    overview.append("## Current Queue")
    if queued_titles:
        overview.extend([f"- `{title}`" for title in queued_titles])
    else:
        overview.append("- none")
    if status_lines:
        overview.append("")
        overview.append("## Live Status Snapshot")
        overview.extend(status_lines)
    if wallet_lines:
        overview.append("")
        overview.append("## Wallet Intel Snapshot")
        overview.extend(wallet_lines)
    overview.append("")
    overview.append("Edit `Arbos/TASKS.md` for canonical task changes.")
    overview.append("")
    write_text(marvin_root / "ARBOS.md", "\n".join(overview))
