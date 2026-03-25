#!/usr/bin/env python3
"""Nova SN68 mining loop — autonomous mining agent.

Chain-aware architecture:
  - Health timer   (wall-clock, 15-30s) — GPU, miner, GitHub, labels, auto-restart
  - Strategy timer (chain-aware)        — fires in submission window (≤20 blocks before epoch end)
  - Research timer (epoch-aligned)      — fires at epoch boundaries (~361 blocks / ~72 min)

File-based interface (Nova/ workspace) syncs with OpenClaw "Const".
SQLite is the source of truth; markdown views are rendered from it.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Brains.chutes_client import call_chutes as call_tracked_chutes
from Brains.nova_loop_chain import (
    ChainAwareScheduler,
    ChainState,
    compute_attribution_window,
    fetch_chain_state,
)
from Brains.nova_loop_config import NovaConfig, parse_args
from Brains.nova_loop_prompts import (
    RESEARCH_SYSTEM_PROMPT,
    build_briefing,
    build_research_prompt,
)
from Brains.nova_loop_proposals import run_strategy_cycle
from Brains.nova_loop_rewards import (
    compute_surrogate_correlation,
    update_rewards,
)
from Brains.nova_loop_safety import SafetyGate
from Brains.nova_loop_ssh import PodSSH
from Brains.nova_loop_state import NovaStateDB, iso_now
from Brains.service_env import load_shared_api_env

log = logging.getLogger(__name__)


# ── utilities ───────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_text(path: Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fallback


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{iso_now()}] {message}\n")


# ── stagnation detection (from Arbos pattern) ──────────────────────────────

def load_loop_state(path: Path) -> dict[str, Any]:
    return read_json(path)


def update_loop_state(
    path: Path,
    *,
    action: str,
    analysis: str,
    confidence: float,
) -> dict[str, Any]:
    """Track decision signatures for stagnation detection."""
    state = load_loop_state(path)
    signature = {"action": action, "analysis": analysis[:100]}

    last = state.get("last_signature") or {}
    repeat_count = int(state.get("repeat_count", 0))
    if last.get("action") == signature["action"] and last.get("analysis") == signature["analysis"]:
        repeat_count += 1
    else:
        repeat_count = 0

    next_state = {
        "updated_at": iso_now(),
        "last_signature": signature,
        "repeat_count": repeat_count,
        "stagnating": repeat_count >= 2,
        "confidence": confidence,
    }
    write_json(path, next_state)
    return next_state


# ── status rendering ────────────────────────────────────────────────────────

def render_status(
    cfg: NovaConfig,
    db: NovaStateDB,
    ssh: PodSSH,
    safety: SafetyGate,
    scheduler: ChainAwareScheduler,
    *,
    gpu_info: dict[str, Any] | None = None,
    miner_alive: bool | None = None,
    github_health: dict[str, Any] | None = None,
) -> str:
    """Render STATUS.md from current state."""
    now = iso_now()
    summary = db.summary()
    safety_state = safety.status_dict()
    chain_state = scheduler.status_dict()
    latest_reward = summary.get("latest_reward") or {}

    lines = [
        "# Nova SN68 Mining Status",
        "",
        f"- Updated at: `{now}`",
        f"- Loop state: `{'PAUSED' if safety_state['paused'] else 'RUNNING'}`",
    ]

    if safety_state["paused"]:
        lines.append(f"- Pause reason: `{safety_state['pause_reason']}`")

    # Chain timing
    lines.extend([
        "",
        "## Chain",
        f"- Block: `{chain_state.get('current_block', '?')}`",
        f"- Epoch: `{chain_state.get('epoch_progress', '?')}`",
        f"- Tempo: `{chain_state.get('tempo_progress', '?')}`",
        f"- Submission window: `{chain_state.get('in_submission_window', '?')}`",
        f"- Blocks to epoch end: `{chain_state.get('blocks_until_epoch_end', '?')}`",
        f"- Blocks to tempo end: `{chain_state.get('blocks_until_tempo_end', '?')}`",
        f"- Chain OK: `{chain_state.get('chain_ok', False)}`",
    ])
    if chain_state.get("chain_error"):
        lines.append(f"- Chain error: `{chain_state['chain_error']}`")

    # GPU
    lines.append("")
    lines.append("## Infrastructure")
    if gpu_info:
        lines.append(f"- GPU: `{gpu_info.get('gpu_name', 'unknown')}` | "
                     f"temp=`{gpu_info.get('temp_c', '?')}C` | "
                     f"util=`{gpu_info.get('util_pct', '?')}%` | "
                     f"mem=`{gpu_info.get('mem_used_mb', '?')}/{gpu_info.get('mem_total_mb', '?')} MB`")
    else:
        lines.append("- GPU: `unknown`")
    lines.append(f"- Miner alive: `{miner_alive if miner_alive is not None else 'unknown'}`")

    # GitHub submission health
    if github_health:
        gh_status = "OK" if github_health.get("ok") else "DEGRADED"
        lines.append(f"- GitHub: `{gh_status}`")
        if github_health.get("auth_ok") is not None:
            lines.append(f"  - Auth: `{github_health['auth_ok']}`")
        if github_health.get("last_push_age_seconds") is not None:
            lines.append(f"  - Last push age: `{github_health['last_push_age_seconds']}s`")
        if github_health.get("error"):
            lines.append(f"  - Error: `{github_health['error']}`")
    else:
        lines.append("- GitHub: `unchecked`")

    # Scores
    if latest_reward:
        lines.extend([
            "",
            "## Scores",
            f"- Boltz score: `{latest_reward.get('our_score', '?')}`",
            f"- Leader score: `{latest_reward.get('leader_score', '?')}`",
            f"- Score gap: `{latest_reward.get('score_gap', '?')}`",
            f"- Rank: `{latest_reward.get('rank', '?')}` / `{latest_reward.get('field_size', '?')}`",
        ])

    # Proposals
    proposal_stats = summary.get("proposals", {})
    if proposal_stats:
        lines.extend(["", "## Proposals"])
        for status, count in sorted(proposal_stats.items()):
            lines.append(f"- {status}: `{count}`")

    # Safety
    lines.extend([
        "",
        "## Safety",
        f"- Restarts this hour: `{safety_state['restarts_this_hour']}/{safety_state['max_restarts_per_hour']}`",
        f"- Target switches today: `{safety_state['target_switches_today']}/{safety_state['max_switches_per_day']}`",
    ])
    if safety_state.get("active_cooldowns"):
        for action, secs in safety_state["active_cooldowns"].items():
            lines.append(f"- Cooldown: `{action}` ({secs}s remaining)")

    # Pending directives
    if summary.get("pending_directives", 0) > 0:
        lines.extend(["", f"## Pending Directives: `{summary['pending_directives']}`"])

    # Recent events
    events = summary.get("recent_events", [])
    if events:
        lines.extend(["", "## Recent Events"])
        for e in events[:5]:
            lines.append(f"- `{e.get('timestamp', '?')}` [{e.get('level', '?')}] {e.get('message', '?')}")

    return "\n".join(lines) + "\n"


# ── inbox parsing ───────────────────────────────────────────────────────────

def check_inbox(inbox_path: Path, db: NovaStateDB) -> list[dict[str, Any]]:
    """Read and consume INBOX.md directives."""
    raw = read_text(inbox_path)
    if not raw.strip():
        return []

    directives = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("- "):
            text = line[2:].strip()
        elif line and not line.startswith("#"):
            text = line
        else:
            continue

        if text:
            directive_id = db.insert_directive(raw_text=text, source="inbox")
            directives.append({"id": directive_id, "raw_text": text, "source": "inbox"})

    write_text(inbox_path, "")
    return directives


# ── outbox writing ──────────────────────────────────────────────────────────

def write_outbox_if_needed(
    outbox_path: Path,
    db: NovaStateDB,
    since: str | None = None,
) -> bool:
    """Append urgent events to OUTBOX.md for Const to relay via Telegram."""
    urgent = db.urgent_events(since=since)
    if not urgent:
        return False

    existing = read_text(outbox_path)
    new_items = []
    for e in urgent:
        new_items.append(f"- [{e.get('timestamp', '?')}] **{e.get('category', '?')}**: {e.get('message', '?')}")

    if new_items:
        content = existing.rstrip() + "\n" + "\n".join(new_items) + "\n" if existing.strip() else "\n".join(new_items) + "\n"
        write_text(outbox_path, content)
        return True

    return False


# ── GitHub submission health ────────────────────────────────────────────────

def check_github_health(ssh: PodSSH) -> dict[str, Any]:
    """Check GitHub API accessibility on the pod.

    Nova submission uses GITHUB_REPO_OWNER, GITHUB_REPO_NAME,
    GITHUB_REPO_BRANCH, and optional GITHUB_REPO_PATH to build the
    upload target, then calls upload_file_to_github(filename, content).
    The miner raises if owner/repo/branch are missing.

    We validate the same env vars and test the actual API path the
    miner constructs (Contents API for the configured branch/path).
    """
    result: dict[str, Any] = {"ok": False}

    # 1. Read all required env vars from the pod in one call
    env_check = ssh.run(
        'printf "OWNER=%s\\nNAME=%s\\nBRANCH=%s\\nPATH=%s\\nTOKEN=%s\\n" '
        '"${GITHUB_REPO_OWNER:-}" '
        '"${GITHUB_REPO_NAME:-}" '
        '"${GITHUB_REPO_BRANCH:-}" '
        '"${GITHUB_REPO_PATH:-}" '
        '"${GITHUB_TOKEN:+set}"',
        timeout=10,
    )
    env_vars: dict[str, str] = {}
    if env_check.ok:
        for line in env_check.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env_vars[k] = v

    result["repo_owner"] = bool(env_vars.get("OWNER"))
    result["repo_name"] = bool(env_vars.get("NAME"))
    result["repo_branch"] = env_vars.get("BRANCH", "")
    result["repo_path"] = env_vars.get("PATH", "")
    result["token_set"] = env_vars.get("TOKEN") == "set"

    # Nova raises if owner, name, or branch are missing
    missing = []
    if not result["repo_owner"]:
        missing.append("GITHUB_REPO_OWNER")
    if not result["repo_name"]:
        missing.append("GITHUB_REPO_NAME")
    if not result.get("repo_branch"):
        missing.append("GITHUB_REPO_BRANCH")
    if not result["token_set"]:
        missing.append("GITHUB_TOKEN")

    if missing:
        result["error"] = f"missing env vars: {', '.join(missing)}"
        return result

    owner = env_vars["OWNER"]
    name = env_vars["NAME"]
    branch = env_vars["BRANCH"]
    path = env_vars.get("PATH", "")

    # 2. Validate repo + branch exists via API
    api_check = ssh.run(
        f'curl -sf -o /dev/null -w "%{{http_code}}" '
        f'-H "Authorization: token $GITHUB_TOKEN" '
        f'"https://api.github.com/repos/{owner}/{name}/branches/{branch}" '
        f'2>/dev/null || echo "000"',
        timeout=15,
    )
    branch_status = api_check.stdout.strip() if api_check.ok else "error"
    result["branch_status"] = branch_status
    result["branch_ok"] = branch_status == "200"

    if not result["branch_ok"]:
        result["error"] = f"branch '{branch}' not found (API {branch_status})"
        return result

    # 3. Validate the contents path the miner uses for upload
    # Nova constructs: /repos/{owner}/{name}/contents/{path}/{filename}
    # We check that the path directory is accessible
    if path:
        path_check = ssh.run(
            f'curl -sf -o /dev/null -w "%{{http_code}}" '
            f'-H "Authorization: token $GITHUB_TOKEN" '
            f'"https://api.github.com/repos/{owner}/{name}/contents/{path}?ref={branch}" '
            f'2>/dev/null || echo "000"',
            timeout=15,
        )
        path_status = path_check.stdout.strip() if path_check.ok else "error"
        result["path_status"] = path_status
        result["path_ok"] = path_status == "200"
        if not result["path_ok"]:
            result["error"] = f"contents path '{path}' not found (API {path_status})"
            return result
    else:
        result["path_ok"] = True  # no path configured = root, always valid

    result["ok"] = True
    return result


# ── health timer ────────────────────────────────────────────────────────────

def run_health_check(
    cfg: NovaConfig,
    db: NovaStateDB,
    ssh: PodSSH,
    safety: SafetyGate,
    scheduler: ChainAwareScheduler,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Deterministic health check — no LLM. Runs every 15-30s (wall clock).

    When dry_run=True, only reads state. Never restarts miner, mutates
    queue files, or probes GitHub (which can have side effects).
    """
    result: dict[str, Any] = {"timestamp": iso_now()}

    # GPU health (read-only)
    gpu_info = ssh.gpu_alive()
    result["gpu"] = gpu_info

    if not gpu_info.get("alive"):
        db.log_event("health", "GPU not responding", level="urgent")
        result["gpu_status"] = "DOWN"
    else:
        temp = gpu_info.get("temp_c", -1)
        if temp > 90:
            db.log_event("health", f"GPU temperature critical: {temp}C", level="urgent")
        elif temp > 80:
            db.log_event("health", f"GPU temperature high: {temp}C", level="warn")
        db.record_metric("gpu_temp", float(temp))
        db.record_metric("gpu_util", float(gpu_info.get("util_pct", 0)))
        result["gpu_status"] = "OK"

    # Miner alive (read-only check)
    miner_alive = ssh.miner_alive()
    result["miner_alive"] = miner_alive

    # Auto-restart is mutating — skip in dry-run
    if not miner_alive and not safety.is_paused and not dry_run:
        db.log_event("health", "Miner process not running", level="warn")
        allowed, reason = safety.can_restart_miner()
        if allowed:
            db.log_event("health", "Auto-restarting miner", level="info")
            restart_result = ssh.restart_miner()
            safety.record_restart()
            result["auto_restart"] = restart_result.ok
            if restart_result.ok:
                db.log_event("health", "Miner auto-restart succeeded", level="info")
            else:
                db.log_event("health", f"Miner auto-restart failed: {restart_result.stderr}", level="urgent")
        else:
            db.log_event("health", f"Cannot auto-restart: {reason}", level="warn")
    elif not miner_alive and dry_run:
        db.log_event("health", "DRY RUN: miner down, would auto-restart", level="info")

    # Label flow (read-only)
    label_count = ssh.label_count()
    label_age = ssh.recent_label_age_seconds()
    result["label_count"] = label_count
    result["label_age_seconds"] = label_age
    db.record_metric("label_count", float(label_count))

    if label_age > 600 and label_count > 0:
        db.log_event("health", f"Label flow stale: newest label is {label_age}s old", level="warn")

    # GitHub submission health — skip in dry-run (touches remote API)
    if not dry_run:
        github_health = check_github_health(ssh)
        result["github"] = github_health
        if not github_health.get("ok"):
            db.log_event("health", f"GitHub submission path degraded: {github_health.get('error', '?')}", level="warn")
    else:
        result["github"] = {"ok": None, "dry_run": True}

    # Write status
    status_md = render_status(
        cfg, db, ssh, safety, scheduler,
        gpu_info=gpu_info,
        miner_alive=miner_alive,
        github_health=result.get("github"),
    )
    write_text(cfg.status_path, status_md)

    return result


# ── research timer ──────────────────────────────────────────────────────────

def run_research_cycle(
    cfg: NovaConfig,
    db: NovaStateDB,
    ssh: PodSSH,
    safety: SafetyGate,
    start_time: float,
) -> dict[str, Any]:
    """Deep research cycle — fires at epoch boundaries (where validator sets weights)."""
    if safety.is_paused:
        return {"skipped": True, "reason": safety.pause_reason}

    uptime_hours = round((time.time() - start_time) / 3600, 1)
    proposal_stats = db.proposal_stats()

    context: dict[str, Any] = {
        "timestamp": iso_now(),
        "uptime_hours": uptime_hours,
        "total_proposals": sum(proposal_stats.values()),
        "succeeded": proposal_stats.get("succeeded", 0),
        "failed": proposal_stats.get("failed", 0),
        "reward_trend": db.reward_history(limit=10),
        "proposal_outcomes": db.recent_proposals(limit=10),
        "surrogate_accuracy": compute_surrogate_correlation(db),
    }

    recent_temps = db.metric_history("gpu_temp", limit=20)
    recent_utils = db.metric_history("gpu_util", limit=20)
    if recent_temps:
        avg_temp = sum(m["value"] for m in recent_temps) / len(recent_temps)
        context["gpu_stats"] = {
            "avg_temp": round(avg_temp, 1),
            "avg_util": round(sum(m["value"] for m in recent_utils) / max(len(recent_utils), 1), 1) if recent_utils else 0,
            "restarts_24h": safety.status_dict().get("restarts_this_hour", 0),
        }

    api_key = cfg.api_key
    if not api_key:
        db.log_event("research", "No API key, skipping research cycle", level="info")
        return {"skipped": True, "reason": "no API key"}

    user_prompt = build_research_prompt(context)

    try:
        raw_response = call_tracked_chutes(
            api_key=api_key,
            model=cfg.llm.research_model,
            system_prompt=RESEARCH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            base_url=cfg.llm.base_url,
            temperature=cfg.llm.research_temperature,
            max_tokens=cfg.llm.research_max_tokens,
            timeout=cfg.llm.timeout,
            retries=cfg.llm.retries,
            usage_action="nova_research",
        )
    except Exception as exc:
        db.log_event("research", f"Chutes research call failed: {exc}", level="warn")
        return {"skipped": False, "error": str(exc)}

    from Brains.nova_loop_proposals import _parse_json_response
    research_response = _parse_json_response(raw_response)

    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    write_text(cfg.runs_dir / f"{stamp}_research_prompt.md", user_prompt)
    write_text(cfg.runs_dir / f"{stamp}_research_response.md", raw_response)

    briefing = build_briefing(context, research_response)
    write_text(cfg.briefing_path, briefing)

    if research_response.get("retrain"):
        db.log_event("research", f"Recommending retrain: {research_response.get('retrain_reason', '?')}", level="info")
    if research_response.get("reallocate"):
        db.log_event("research", f"Recommending reallocate: {research_response.get('reallocate_reason', '?')}", level="info")

    db.log_event("research", f"Research cycle complete: {research_response.get('briefing_summary', 'no summary')}", level="info")

    return {
        "skipped": False,
        "retrain": research_response.get("retrain", False),
        "reallocate": research_response.get("reallocate", False),
        "summary": research_response.get("briefing_summary", ""),
    }


# ── main loop ───────────────────────────────────────────────────────────────

def validate_model_ids(cfg: NovaConfig) -> None:
    """Validate model IDs against the Chutes /v1/models endpoint at startup.

    Fails fast with a clear error instead of surfacing bad IDs later
    during a strategy or research cycle.
    """
    import urllib.request
    import urllib.error

    api_key = cfg.api_key
    if not api_key:
        log.warning("No API key — skipping model ID validation")
        return

    # Chutes lists models at GET /v1/models (OpenAI-compatible)
    models_url = cfg.llm.base_url.replace("/chat/completions", "/models")
    req = urllib.request.Request(
        models_url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning("Could not validate model IDs (Chutes unreachable): %s", exc)
        return

    available_ids = {m["id"] for m in data.get("data", [])}
    if not available_ids:
        log.warning("Chutes returned no models — skipping validation")
        return

    for label, model_id in [
        ("strategy", cfg.llm.strategy_model),
        ("research", cfg.llm.research_model),
    ]:
        if not model_id:
            log.error("No %s model configured (--strategy-model / --research-model required)", label)
            raise SystemExit(1)
        if model_id not in available_ids:
            log.error(
                "%s model '%s' not found on Chutes. Available: %s",
                label, model_id,
                ", ".join(sorted(available_ids)[:10]),
            )
            raise SystemExit(1)
        log.info("Validated %s model: %s", label, model_id)


def main() -> int:
    cfg = parse_args()
    load_shared_api_env()

    cfg.ensure_dirs()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(cfg.loop_log_path), encoding="utf-8"),
        ],
    )

    # Validate model IDs before starting (fail fast on bad config)
    if not cfg.dry_run:
        validate_model_ids(cfg)

    # Initialize components
    db = NovaStateDB(cfg.db_path)
    ssh = PodSSH(cfg.ssh)
    safety = SafetyGate(cfg.safety, db)
    scheduler = ChainAwareScheduler(
        strategy_fallback_seconds=cfg.timers.strategy_seconds,
        research_fallback_seconds=cfg.timers.research_seconds,
    )

    startup_msg = (
        f"nova-loop starting | "
        f"health={cfg.timers.health_seconds}s (wall-clock) | "
        f"strategy=chain-aware (fallback {cfg.timers.strategy_seconds}s) | "
        f"research=epoch-aligned (fallback {cfg.timers.research_seconds}s) | "
        f"budget={cfg.llm.budget_per_day}"
    )
    print(startup_msg, flush=True)
    log_line(cfg.loop_log_path, startup_msg)
    db.log_event("system", startup_msg, level="info")

    start_time = time.time()
    last_health = 0.0
    last_outbox_check = iso_now()
    chain_state = ChainState()  # empty until first fetch

    while True:
        now = time.time()
        try:
            # ── Health timer (wall-clock, deterministic) ────────────
            if now - last_health >= max(cfg.timers.health_seconds, 1.0):
                # Fetch chain state first so health/status have fresh data
                chain_state = fetch_chain_state(ssh)
                # Update scheduler's cached chain state so render_status is current
                scheduler.last_chain_state = chain_state

                health_result = run_health_check(cfg, db, ssh, safety, scheduler, dry_run=cfg.dry_run)
                last_health = now

                # Process INBOX directives every health cycle (decoupled from strategy)
                check_inbox(cfg.inbox_path, db)
                directives = db.pending_directives()
                for d in directives:
                    safety_result = safety.handle_directive(d.get("raw_text", ""))
                    if safety_result:
                        db.apply_directive(d["id"], notes=f"safety: {safety_result}")
                    # Non-safety directives stay pending for the LLM in strategy
                if chain_state.ok:
                    db.record_metric("block", float(chain_state.current_block))

                gpu_status = health_result.get("gpu_status", "?")
                miner_alive = health_result.get("miner_alive", "?")
                label_count = health_result.get("label_count", 0)
                gh_ok = health_result.get("github", {}).get("ok", "?")
                auto_restart = health_result.get("auto_restart")

                summary_parts = [
                    f"gpu={gpu_status}",
                    f"miner={miner_alive}",
                    f"gh={gh_ok}",
                    f"labels={label_count}",
                    f"block={chain_state.current_block}",
                    f"epoch={chain_state.blocks_into_epoch}/{chain_state.epoch_length}",
                ]
                if auto_restart is not None:
                    summary_parts.append(f"auto_restart={'ok' if auto_restart else 'failed'}")
                summary = " | ".join(summary_parts)

                print(f"[{iso_now()}] health | {summary}", flush=True)
                log_line(cfg.loop_log_path, f"health | {summary}")

            # ── Strategy timer (chain-aware: submission window) ─────
            should_strategy, strategy_reason = scheduler.should_run_strategy(chain_state)
            if should_strategy:
                reward_snapshot = update_rewards(db, ssh)

                loop_state = load_loop_state(cfg.loop_state_path)
                stagnation = loop_state if loop_state.get("stagnating") else None

                log_line(cfg.loop_log_path, f"starting strategy cycle | reason={strategy_reason}")

                chain_dict = scheduler.status_dict() if chain_state.ok else None

                if cfg.dry_run:
                    strategy_result: dict[str, Any] = {"skipped": False, "dry_run": True}
                    db.log_event("strategy", f"DRY RUN: {strategy_reason}", level="info")
                else:
                    strategy_result = run_strategy_cycle(
                        db=db, ssh=ssh, safety=safety, cfg=cfg,
                        stagnation=stagnation, chain_state=chain_dict,
                        chain_state_obj=chain_state,
                    )

                scheduler.record_strategy_run(chain_state)

                action = strategy_result.get("action", "none")
                analysis = strategy_result.get("analysis", "")
                confidence = strategy_result.get("confidence", 0.0)
                next_loop_state = update_loop_state(
                    cfg.loop_state_path,
                    action=action,
                    analysis=analysis,
                    confidence=confidence,
                )

                stamp = utc_now().strftime("%Y%m%d_%H%M%S")
                write_text(
                    cfg.runs_dir / f"{stamp}_strategy.json",
                    json.dumps(strategy_result, indent=2, default=str),
                )

                if strategy_result.get("skipped"):
                    summary = f"strategy skipped: {strategy_result.get('reason', '?')}"
                else:
                    summary = (
                        f"strategy | reason={strategy_reason} | "
                        f"block={chain_state.current_block} | "
                        f"proposals={strategy_result.get('proposals_generated', 0)} | "
                        f"selected={strategy_result.get('selected', 'none')} | "
                        f"action={strategy_result.get('action', 'none')} | "
                        f"executed={strategy_result.get('executed', '?')}"
                    )
                print(f"[{iso_now()}] {summary}", flush=True)
                log_line(cfg.loop_log_path, summary)

                if next_loop_state.get("stagnating"):
                    stag_msg = f"stagnation-alert | repeat_count={next_loop_state.get('repeat_count', 0) + 1}"
                    print(f"[{iso_now()}] {stag_msg}", flush=True)
                    log_line(cfg.loop_log_path, stag_msg)

                write_outbox_if_needed(cfg.outbox_path, db, since=last_outbox_check)
                last_outbox_check = iso_now()

                write_text(cfg.status_path, render_status(cfg, db, ssh, safety, scheduler))

            # ── Research timer (tempo-aligned) ──────────────────────
            should_research, research_reason = scheduler.should_run_research(chain_state)
            if should_research:
                log_line(cfg.loop_log_path, f"starting research cycle | reason={research_reason}")

                research_result = run_research_cycle(cfg, db, ssh, safety, start_time)
                scheduler.record_research_run(chain_state)

                if research_result.get("skipped"):
                    summary = f"research skipped: {research_result.get('reason', '?')}"
                else:
                    summary = (
                        f"research | reason={research_reason} | "
                        f"retrain={research_result.get('retrain', False)} | "
                        f"reallocate={research_result.get('reallocate', False)} | "
                        f"summary={research_result.get('summary', '?')[:80]}"
                    )
                print(f"[{iso_now()}] {summary}", flush=True)
                log_line(cfg.loop_log_path, summary)

        except KeyboardInterrupt:
            print("nova-loop stopped by user", flush=True)
            log_line(cfg.loop_log_path, "stopped by user")
            db.log_event("system", "stopped by user", level="info")
            db.close()
            return 0
        except Exception as exc:
            print(f"[{iso_now()}] loop error: {exc}", flush=True)
            traceback.print_exc()
            log_line(cfg.loop_log_path, f"loop error: {exc}")
            db.log_event("system", f"loop error: {exc}", level="error")

        if cfg.run_once:
            db.close()
            return 0
        time.sleep(max(cfg.timers.cycle_seconds, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
