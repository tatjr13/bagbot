"""Proposal engine: generate, score, filter, execute, and track proposals.

Pipeline: LLM proposes → deterministic scorer → safety filter → commit →
execute → monitor → attribute.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from Brains.chutes_client import call_chutes as call_tracked_chutes
from Brains.nova_loop_chain import ChainState, compute_attribution_window
from Brains.nova_loop_config import NovaConfig
from Brains.nova_loop_prompts import (
    STRATEGY_SYSTEM_PROMPT,
    build_strategy_prompt,
)
from Brains.nova_loop_safety import SafetyGate
from Brains.nova_loop_ssh import PodSSH
from Brains.nova_loop_state import NovaStateDB, iso_now

log = logging.getLogger(__name__)


# ── snapshot building ───────────────────────────────────────────────────────

def build_state_snapshot(
    db: NovaStateDB,
    ssh: PodSSH,
    safety: SafetyGate,
    chain_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gather everything the LLM needs for a strategy decision."""
    # GPU/miner health
    gpu_info = ssh.gpu_alive()
    miner_alive = ssh.miner_alive()
    label_count = ssh.label_count()
    label_age = ssh.recent_label_age_seconds()

    # Reward state
    latest_reward = db.latest_reward_snapshot()

    # Recent proposals
    recent = db.recent_proposals(limit=5)

    # Pending directives
    directives = db.pending_directives()

    # Safety state
    safety_state = safety.status_dict()

    return {
        "timestamp": iso_now(),
        "gpu_alive": gpu_info.get("alive", False),
        "gpu_temp": gpu_info.get("temp_c", -1),
        "gpu_util": gpu_info.get("util_pct", -1),
        "gpu_mem_used": gpu_info.get("mem_used_mb", -1),
        "gpu_mem_total": gpu_info.get("mem_total_mb", -1),
        "miner_alive": miner_alive,
        "label_count": label_count,
        "label_age_seconds": label_age,
        "chain": chain_state,
        "reward": dict(latest_reward) if latest_reward else None,
        "recent_proposals": [dict(p) for p in recent],
        "directives": [dict(d) for d in directives],
        "safety": safety_state,
    }


# ── LLM proposal generation ────────────────────────────────────────────────

def generate_proposals(
    snapshot: dict[str, Any],
    cfg: NovaConfig,
    *,
    temperature: float | None = None,
    stagnation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Chutes to generate proposals from the current state.

    Returns parsed JSON response or a fallback dict on error.
    """
    if stagnation:
        snapshot["stagnation"] = stagnation

    user_prompt = build_strategy_prompt(snapshot)
    effective_temp = temperature or cfg.llm.strategy_temperature

    api_key = cfg.api_key
    if not api_key:
        return {
            "analysis": "No API key available",
            "proposals": [],
            "recommended": None,
            "confidence": 0.0,
            "hold_reason": "CHUTES_API_KEY not set",
        }

    try:
        raw = call_tracked_chutes(
            api_key=api_key,
            model=cfg.llm.strategy_model,
            system_prompt=STRATEGY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            base_url=cfg.llm.base_url,
            temperature=effective_temp,
            max_tokens=cfg.llm.strategy_max_tokens,
            timeout=cfg.llm.timeout,
            retries=cfg.llm.retries,
            usage_action="nova_strategy",
        )
        # Try to extract JSON from the response (might have markdown fences)
        return _parse_json_response(raw)
    except Exception as exc:
        log.error("Chutes strategy call failed: %s", exc)
        return {
            "analysis": f"LLM call failed: {exc}",
            "proposals": [],
            "recommended": None,
            "confidence": 0.0,
            "hold_reason": str(exc),
        }


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Extract JSON from a response that might have markdown code fences."""
    text = raw.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {
            "analysis": "Failed to parse LLM response as JSON",
            "proposals": [],
            "recommended": None,
            "confidence": 0.0,
            "hold_reason": f"Parse error. Raw response starts with: {raw[:200]}",
            "_raw": raw[:500],
        }


# ── scoring ─────────────────────────────────────────────────────────────────

def score_proposals(
    proposals: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deterministic scoring of LLM proposals.

    Each proposal gets a 'score' field (0.0-1.0). Higher = better.
    """
    scored = []
    for p in proposals:
        score = _compute_proposal_score(p, snapshot)
        scored.append({**p, "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# Allowed action types — intentionally narrow.
# Broader autonomy (switch_target, adjust_labels, deploy_model) only
# after attribution aligns with tempo outcomes.
ALLOWED_ACTIONS = {"source_tune", "submit_hold", "retrain_decision", "restart_miner"}


def _compute_proposal_score(proposal: dict[str, Any], snapshot: dict[str, Any]) -> float:
    """Deterministic scoring heuristic for the narrow action set."""
    base = 0.5

    action = proposal.get("action", "")

    # Reject unknown actions
    if action not in ALLOWED_ACTIONS:
        return 0.0

    # Risk penalty
    risk = proposal.get("risk", "low")
    risk_penalty = {"low": 0.0, "medium": 0.15, "high": 0.35}.get(risk, 0.2)
    base -= risk_penalty

    # Action type bonuses
    action_bonus = {
        "submit_hold": 0.1,       # conservative default (hold is the safe choice)
        "source_tune": 0.05,      # low risk
        "retrain_decision": 0.0,  # neutral
        "restart_miner": -0.1,    # disruptive, only when needed
    }.get(action, 0.0)
    base += action_bonus

    if proposal.get("rollback"):
        base += 0.05

    delay = proposal.get("expected_delay_min", 5)
    if delay > 30:
        base -= 0.1
    elif delay > 15:
        base -= 0.05

    return max(0.0, min(1.0, base))


# ── safety filter ───────────────────────────────────────────────────────────

def filter_proposals(
    proposals: list[dict[str, Any]],
    safety: SafetyGate,
) -> list[dict[str, Any]]:
    """Remove proposals that violate safety constraints."""
    filtered = []
    for p in proposals:
        action = p.get("action", "")
        allowed, reason = safety.check_action(action)
        if allowed:
            filtered.append(p)
        else:
            log.info("Proposal %s filtered out: %s", p.get("id", "?"), reason)
    return filtered


# ── execution ───────────────────────────────────────────────────────────────

def execute_proposal(
    proposal: dict[str, Any],
    *,
    db: NovaStateDB,
    ssh: PodSSH,
    safety: SafetyGate,
    cfg: NovaConfig,
    chain_state: ChainState | None = None,
) -> bool:
    """Execute a single proposal. Returns True on success."""
    proposal_id = proposal.get("id", f"prop_{uuid.uuid4().hex[:8]}")
    action = proposal.get("action", "unknown")
    params = proposal.get("params", {})

    # Record the proposal with tempo-aligned attribution window
    current_reward = db.latest_reward_snapshot()
    score_before = current_reward.get("our_score") if current_reward else None

    attribution_window = proposal.get("attribution_window_min", 30)
    if chain_state is not None:
        attribution_window = compute_attribution_window(chain_state)

    db.insert_proposal(
        proposal_id=proposal_id,
        timer="strategy",
        action=action,
        params=params,
        expected_delay_min=proposal.get("expected_delay_min", 5),
        risk=proposal.get("risk", "low"),
        attribution_window_min=attribution_window,
        rollback=proposal.get("rollback"),
        score_before=score_before,
    )
    db.commit_proposal(proposal_id)

    try:
        success = _execute_action(action, params, ssh=ssh, safety=safety, cfg=cfg)
        db.execute_proposal(proposal_id)
        if success:
            db.log_event("strategy", f"Executed proposal {proposal_id}: {action}", level="info")
        else:
            db.resolve_proposal(proposal_id, success=False, notes="Execution returned False")
            safety.set_cooldown(action)
        return success
    except Exception as exc:
        log.error("Proposal %s execution failed: %s", proposal_id, exc)
        db.resolve_proposal(proposal_id, success=False, notes=str(exc))
        safety.set_cooldown(action)
        return False


def _execute_action(
    action: str,
    params: dict[str, Any],
    *,
    ssh: PodSSH,
    safety: SafetyGate,
    cfg: NovaConfig,
) -> bool:
    """Dispatch an action to its handler.

    Intentionally narrow action set. Broader autonomy (switch_target,
    adjust_labels, deploy_model) is gated behind having tempo-aligned
    attribution data.
    """

    if action == "submit_hold":
        # Decide whether to submit or hold position.
        # hold=true means keep current submission (preserve tiebreaker).
        hold = params.get("hold", True)
        if hold:
            log.info("Holding position (preserving tiebreaker advantage)")
            return True
        # Submit: write candidate info for the miner to pick up
        candidate_id = params.get("candidate_id", "")
        if not candidate_id:
            log.warning("submit_hold with hold=false but no candidate_id")
            return False
        result = ssh.run(f"echo '{candidate_id}' >> /tmp/nova_submit_queue.txt", timeout=10)
        return result.ok

    elif action == "restart_miner":
        safety.record_restart()
        start_cmd = params.get("start_cmd", "")
        result = ssh.restart_miner(start_cmd)
        return result.ok

    elif action == "source_tune":
        tune_json = json.dumps(params)
        result = ssh.run(f"echo '{tune_json}' > /tmp/nova_source_tune.json", timeout=10)
        return result.ok

    elif action == "retrain_decision":
        retrain = params.get("retrain", False)
        if not retrain:
            log.info("Retrain decision: no retrain needed")
            return True
        result = ssh.run("cd /root/nova && python3 retrain_surrogate.py &", timeout=15)
        return result.ok

    else:
        log.warning("Rejected unknown action: %s (allowed: %s)", action, ALLOWED_ACTIONS)
        return False


# ── attribution ─────────────────────────────────────────────────────────────

def process_matured_proposals(db: NovaStateDB) -> int:
    """Check proposals whose attribution window has passed and resolve them.

    Returns number of proposals resolved.
    """
    matured = db.matured_proposals()
    resolved = 0
    for prop in matured:
        proposal_id = prop["id"]
        score_before = prop.get("score_before")

        # Get current score
        current = db.latest_reward_snapshot()
        score_after = current.get("our_score") if current else None

        # Compute reward
        reward = None
        if score_before is not None and score_after is not None:
            reward = score_after - score_before

        success = reward is not None and reward >= 0
        db.resolve_proposal(
            proposal_id,
            success=success,
            score_after=score_after,
            reward=reward,
            notes="attribution window matured",
        )
        resolved += 1
        log.info(
            "Resolved proposal %s: reward=%s success=%s",
            proposal_id, reward, success,
        )

    return resolved


# ── top-level strategy cycle ────────────────────────────────────────────────

def run_strategy_cycle(
    *,
    db: NovaStateDB,
    ssh: PodSSH,
    safety: SafetyGate,
    cfg: NovaConfig,
    stagnation: dict[str, Any] | None = None,
    chain_state: dict[str, Any] | None = None,
    chain_state_obj: ChainState | None = None,
) -> dict[str, Any]:
    """Execute one full strategy cycle. Returns a summary dict."""
    if safety.is_paused:
        return {"skipped": True, "reason": safety.pause_reason}

    # Process matured proposals first
    matured_count = process_matured_proposals(db)

    # Build snapshot (directives are processed in the main health loop now,
    # but pending non-safety ones are still included in the snapshot for LLM)
    snapshot = build_state_snapshot(db, ssh, safety, chain_state=chain_state)

    if safety.is_paused:
        return {"skipped": True, "reason": safety.pause_reason}

    # Generate proposals
    temperature = cfg.llm.strategy_temperature
    if stagnation and stagnation.get("stagnating"):
        temperature += cfg.llm.stagnation_temp_boost

    llm_response = generate_proposals(snapshot, cfg, temperature=temperature, stagnation=stagnation)

    proposals = llm_response.get("proposals", [])
    recommended = llm_response.get("recommended")

    if not proposals:
        db.log_event("strategy", f"No proposals. Hold reason: {llm_response.get('hold_reason', 'none')}", level="info")
        return {
            "skipped": False,
            "proposals_generated": 0,
            "matured_resolved": matured_count,
            "analysis": llm_response.get("analysis", ""),
            "hold_reason": llm_response.get("hold_reason", ""),
        }

    # Score and filter
    scored = score_proposals(proposals, snapshot)
    filtered = filter_proposals(scored, safety)

    if not filtered:
        db.log_event("strategy", "All proposals filtered out by safety", level="info")
        return {
            "skipped": False,
            "proposals_generated": len(proposals),
            "proposals_filtered": len(proposals),
            "matured_resolved": matured_count,
        }

    # Pick the best (prefer LLM's recommendation if it survived filtering)
    selected = filtered[0]
    if recommended:
        for p in filtered:
            if p.get("id") == recommended:
                selected = p
                break

    # Execute
    if cfg.dry_run:
        db.log_event("strategy", f"DRY RUN: would execute {selected.get('id', '?')}: {selected.get('action', '?')}", level="info")
        success = True
    else:
        success = execute_proposal(
            selected, db=db, ssh=ssh, safety=safety, cfg=cfg,
            chain_state=chain_state_obj,
        )

    return {
        "skipped": False,
        "proposals_generated": len(proposals),
        "proposals_filtered": len(proposals) - len(filtered),
        "selected": selected.get("id", "?"),
        "action": selected.get("action", "?"),
        "executed": success,
        "matured_resolved": matured_count,
        "analysis": llm_response.get("analysis", ""),
        "confidence": llm_response.get("confidence", 0.0),
    }
