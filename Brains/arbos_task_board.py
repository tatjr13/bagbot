"""Small markdown task-board helper for Arbos."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SECTION_ORDER = ("Active", "Queued", "Blocked", "Completed")
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class TaskBlock:
    title: str
    body: str

    def field(self, name: str, default: str = "") -> str:
        match = re.search(rf"(?m)^- {re.escape(name)}:\s*(.+)$", self.body)
        return match.group(1).strip() if match else default

    def with_status(self, status: str) -> "TaskBlock":
        body = self.body
        if re.search(r"(?m)^- status:\s*.+$", body):
            body = re.sub(r"(?m)^- status:\s*.+$", f"- status: {status}", body, count=1)
        else:
            lines = body.splitlines()
            insert_at = 1 if lines else 0
            lines.insert(insert_at, f"- status: {status}")
            body = "\n".join(lines)
        return TaskBlock(title=self.title, body=body)


@dataclass
class TaskBoard:
    header: str
    sections: dict[str, list[TaskBlock]]


def _split_sections(text: str) -> tuple[str, dict[str, str]]:
    section_matches = list(re.finditer(r"(?m)^## (Active|Queued|Blocked|Completed)\s*$", text))
    if not section_matches:
        return text, {name: "" for name in SECTION_ORDER}

    header = text[: section_matches[0].start()].rstrip() + "\n\n"
    sections: dict[str, str] = {}
    for idx, match in enumerate(section_matches):
        name = match.group(1)
        start = match.end()
        end = section_matches[idx + 1].start() if idx + 1 < len(section_matches) else len(text)
        sections[name] = text[start:end].strip("\n")
    for name in SECTION_ORDER:
        sections.setdefault(name, "")
    return header, sections


def _parse_blocks(section_text: str) -> list[TaskBlock]:
    if not section_text.strip() or section_text.strip() == "- none":
        return []
    matches = list(re.finditer(r"(?m)^### (.+)$", section_text))
    if not matches:
        return []
    blocks: list[TaskBlock] = []
    for idx, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(section_text)
        body = section_text[start:end].strip("\n")
        blocks.append(TaskBlock(title=title, body=body))
    return blocks


def load_task_board(path: Path) -> TaskBoard:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header, raw_sections = _split_sections(text)
    return TaskBoard(
        header=header,
        sections={name: _parse_blocks(raw_sections[name]) for name in SECTION_ORDER},
    )


def render_task_board(board: TaskBoard) -> str:
    parts = [board.header.rstrip(), ""]
    for name in SECTION_ORDER:
        parts.append(f"## {name}")
        parts.append("")
        blocks = board.sections.get(name, [])
        if blocks:
            for block in blocks:
                parts.append(block.body.rstrip())
                parts.append("")
        else:
            parts.append("- none")
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def save_task_board(path: Path, board: TaskBoard) -> None:
    path.write_text(render_task_board(board), encoding="utf-8")


def append_outbox_note(outbox_path: Path, message: str) -> None:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    note = f"- {stamp} {message}"
    text = outbox_path.read_text(encoding="utf-8") if outbox_path.exists() else "# Arbos Outbox\n\n## Pending Relay\n- none\n"
    if "## Pending Relay" in text:
        text = re.sub(
            r"(?ms)(^## Pending Relay\s*)(.*?)(^(## |\Z))",
            lambda m: m.group(1) + note + ("\n" + m.group(2).strip() if m.group(2).strip() and m.group(2).strip() != "- none" else "") + "\n\n" + m.group(3),
            text,
            count=1,
        )
    else:
        text = text.rstrip() + f"\n\n## Pending Relay\n{note}\n"
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    outbox_path.write_text(text, encoding="utf-8")


def choose_focus_task(board: TaskBoard) -> TaskBlock | None:
    non_timed_active = [task for task in board.sections["Active"] if task.field("type", "").strip().lower() != "timed"]
    if non_timed_active:
        return non_timed_active[0]
    if board.sections["Active"]:
        return board.sections["Active"][0]
    return None


def promote_queued_task(tasks_path: Path, outbox_path: Path | None = None) -> tuple[bool, str | None]:
    board = load_task_board(tasks_path)
    existing_focus = choose_focus_task(board)
    if existing_focus and existing_focus.field("type", "").strip().lower() != "timed":
        return False, existing_focus.title

    queued = board.sections["Queued"]
    if not queued:
        focus = choose_focus_task(board)
        return False, focus.title if focus else None

    def sort_key(task: TaskBlock) -> tuple[int, int]:
        priority = task.field("priority", "medium").strip().lower()
        return PRIORITY_ORDER.get(priority, 99), queued.index(task)

    next_task = sorted(queued, key=sort_key)[0]
    queued.remove(next_task)
    promoted = next_task.with_status("active")
    board.sections["Active"].append(promoted)
    save_task_board(tasks_path, board)
    if outbox_path is not None:
        append_outbox_note(outbox_path, f"promoted `{promoted.title}` from queued to active")
    return True, promoted.title


def task_snapshot(tasks_path: Path) -> dict[str, object]:
    board = load_task_board(tasks_path)
    active_titles = [task.title for task in board.sections["Active"]]
    queued_titles = [task.title for task in board.sections["Queued"]]
    focus = choose_focus_task(board)
    return {
        "focus": focus.title if focus else "none",
        "active_titles": active_titles,
        "queued_titles": queued_titles,
        "active_count": len(active_titles),
        "queued_count": len(queued_titles),
    }
