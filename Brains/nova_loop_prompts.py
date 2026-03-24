"""LLM prompt templates for the Nova mining loop.

Strategy prompts (Qwen3.5-122B) and research prompts (DeepSeek-V3.2).
Output schemas are described in the prompts themselves so the LLM
returns structured JSON proposals.
"""

from __future__ import annotations

from typing import Any


# ── system prompts ──────────────────────────────────────────────────────────

STRATEGY_SYSTEM_PROMPT = """\
You are the strategy engine for a Bittensor SN68 (Nova) mining operation.

Your job: evaluate the current mining state and propose ONE concrete action
to improve our competitive position on the subnet.

SN68 validator scoring (from the public validator code):
- Winner selection: highest ROUNDED boltz_score per target per tempo
- Tiebreaker: earliest block_submitted, then push_time
- Weight: 0.722 burned, remaining weight assigned to winner_boltz
- One winner per target per tempo (~360 blocks / ~72 min)

Submission path: miner encrypts candidate → commits to chain → uploads to GitHub.
Replacing a submission resets block_submitted, losing tiebreaker advantage.
Only replace if the new rounded boltz_score is strictly higher.

You MUST respond with valid JSON matching this schema:
{
  "analysis": "1-2 sentence assessment of current state",
  "proposals": [
    {
      "id": "prop_NNN",
      "action": "<action_type>",
      "params": {},
      "expected_delay_min": <int>,
      "risk": "low|medium|high",
      "attribution_window_min": <int>,
      "rollback": {"action": "<rollback_action>", "params": {}}
    }
  ],
  "recommended": "<proposal_id>",
  "confidence": <0.0-1.0>,
  "hold_reason": "<if recommending no action, explain why>"
}

Valid action types (intentionally narrow):
- source_tune: Adjust molecule source parameters (top_k, diversity, etc.)
- submit_hold: Decide whether to submit a new candidate or hold current position
- retrain_decision: Recommend for/against surrogate model retraining
- restart_miner: Restart the miner process (only if unhealthy)

Rules:
- NEVER propose high-risk actions without strong evidence
- Default to "submit_hold" with hold=true when our boltz_score is competitive
- If proposing a replacement, the new rounded boltz_score must be strictly higher
- "restart_miner" is only for when the miner is unhealthy, not for strategy
- Be specific in params — no vague "optimize" or "improve"
"""

RESEARCH_SYSTEM_PROMPT = """\
You are the long-horizon research engine for a Bittensor SN68 (Nova) mining operation.

Your job: analyze accumulated data, identify trends, and recommend strategic
adjustments that play out over hours/days rather than minutes.

Areas to consider:
- Surrogate model accuracy: Is our XGB screener correlating with Boltz2 ground truth?
- Competitive landscape: Are we gaining or losing ground? Who are the top miners?
- Resource efficiency: GPU utilization, label flow rates, model inference times
- Target selection: Which proteins should we prioritize?
- Infrastructure: Any reliability issues to address?

You MUST respond with valid JSON:
{
  "analysis": "2-3 sentence strategic assessment",
  "trends": [
    {"metric": "<name>", "direction": "up|down|flat", "significance": "low|medium|high"}
  ],
  "recommendations": [
    {
      "action": "<action_type>",
      "rationale": "<why>",
      "priority": "low|medium|high",
      "timeline": "<when to act>"
    }
  ],
  "retrain": <true|false>,
  "retrain_reason": "<if true, why>",
  "reallocate": <true|false>,
  "reallocate_reason": "<if true, why>",
  "briefing_summary": "1-2 sentence summary for the daily briefing"
}
"""


# ── user prompt builders ────────────────────────────────────────────────────

def build_strategy_prompt(snapshot: dict[str, Any]) -> str:
    """Build the user prompt for a strategy cycle."""
    sections = []

    sections.append("## Current State")
    sections.append(f"- Timestamp: {snapshot.get('timestamp', 'unknown')}")
    sections.append(f"- GPU alive: {snapshot.get('gpu_alive', 'unknown')}")
    sections.append(f"- GPU temp: {snapshot.get('gpu_temp', 'unknown')}C")
    sections.append(f"- Miner alive: {snapshot.get('miner_alive', 'unknown')}")
    sections.append(f"- Label count: {snapshot.get('label_count', 'unknown')}")
    sections.append(f"- Label age (seconds): {snapshot.get('label_age_seconds', 'unknown')}")

    # Chain timing
    if snapshot.get("chain"):
        c = snapshot["chain"]
        sections.append("\n## Chain Timing")
        sections.append(f"- Block: {c.get('current_block', 'unknown')}")
        sections.append(f"- Epoch progress: {c.get('epoch_progress', 'unknown')}")
        sections.append(f"- Tempo progress: {c.get('tempo_progress', 'unknown')}")
        sections.append(f"- In submission window: {c.get('in_submission_window', 'unknown')}")
        sections.append(f"- Blocks to epoch end: {c.get('blocks_until_epoch_end', 'unknown')}")

    if snapshot.get("reward"):
        r = snapshot["reward"]
        sections.append("\n## Scoring (Validator Path)")
        sections.append(f"- Boltz score: {r.get('our_score', 'unknown')}")
        sections.append(f"- Leader boltz score: {r.get('leader_score', 'unknown')}")
        sections.append(f"- Score gap: {r.get('score_gap', 'unknown')}")
        sections.append(f"- Rank: {r.get('rank', 'unknown')} / {r.get('field_size', 'unknown')}")
        if r.get('block_submitted'):
            sections.append(f"- Block submitted: {r.get('block_submitted')} (tiebreaker)")

    if snapshot.get("recent_proposals"):
        sections.append("\n## Recent Proposals")
        for p in snapshot["recent_proposals"][:5]:
            sections.append(
                f"- {p.get('id', '?')}: {p.get('action', '?')} | "
                f"status={p.get('status', '?')} | reward={p.get('reward', 'pending')}"
            )

    if snapshot.get("directives"):
        sections.append("\n## Pending Directives")
        for d in snapshot["directives"]:
            sections.append(f"- [{d.get('source', '?')}] {d.get('raw_text', '?')}")

    if snapshot.get("safety"):
        s = snapshot["safety"]
        sections.append("\n## Safety State")
        sections.append(f"- Paused: {s.get('paused', False)}")
        sections.append(f"- Restarts this hour: {s.get('restarts_this_hour', 0)}/{s.get('max_restarts_per_hour', 3)}")
        sections.append(f"- Target switches today: {s.get('target_switches_today', 0)}/{s.get('max_switches_per_day', 2)}")
        if s.get("active_cooldowns"):
            for action, secs in s["active_cooldowns"].items():
                sections.append(f"- Cooldown: {action} ({secs}s remaining)")

    if snapshot.get("stagnation"):
        sections.append("\n## STAGNATION ALERT")
        sections.append(
            f"The loop has repeated the same decision for {snapshot['stagnation'].get('repeat_count', 0)} cycles. "
            "Propose a materially different tactic."
        )

    return "\n".join(sections)


def build_research_prompt(context: dict[str, Any]) -> str:
    """Build the user prompt for a research cycle."""
    sections = []

    sections.append("## Mining Performance Summary")
    sections.append(f"- Uptime: {context.get('uptime_hours', 'unknown')} hours")
    sections.append(f"- Total proposals: {context.get('total_proposals', 0)}")
    sections.append(f"- Successful: {context.get('succeeded', 0)}")
    sections.append(f"- Failed: {context.get('failed', 0)}")

    if context.get("reward_trend"):
        sections.append("\n## Reward Trend")
        for snap in context["reward_trend"][:10]:
            sections.append(
                f"- {snap.get('recorded_at', '?')}: score={snap.get('our_score', '?')} "
                f"gap={snap.get('score_gap', '?')} rank={snap.get('rank', '?')}"
            )

    if context.get("proposal_outcomes"):
        sections.append("\n## Recent Proposal Outcomes")
        for p in context["proposal_outcomes"][:10]:
            sections.append(
                f"- {p.get('id', '?')}: {p.get('action', '?')} → {p.get('status', '?')} "
                f"(reward={p.get('reward', 'pending')})"
            )

    if context.get("gpu_stats"):
        g = context["gpu_stats"]
        sections.append("\n## GPU Health")
        sections.append(f"- Avg temp: {g.get('avg_temp', '?')}C")
        sections.append(f"- Avg utilization: {g.get('avg_util', '?')}%")
        sections.append(f"- Restarts in last 24h: {g.get('restarts_24h', 0)}")

    if context.get("surrogate_accuracy"):
        sections.append(f"\n## Surrogate Model Accuracy")
        sections.append(f"- Correlation: {context['surrogate_accuracy'].get('correlation', '?')}")
        sections.append(f"- Sample size: {context['surrogate_accuracy'].get('n_samples', '?')}")

    return "\n".join(sections)


def build_briefing(context: dict[str, Any], research_response: dict[str, Any]) -> str:
    """Render a daily briefing markdown from research results."""
    lines = [
        "# Nova Daily Briefing",
        "",
        f"- Generated: {context.get('timestamp', 'unknown')}",
        f"- Uptime: {context.get('uptime_hours', 'unknown')} hours",
        "",
        "## Summary",
        research_response.get("briefing_summary", "No summary available."),
        "",
        "## Analysis",
        research_response.get("analysis", "No analysis available."),
        "",
    ]

    trends = research_response.get("trends", [])
    if trends:
        lines.append("## Trends")
        for t in trends:
            lines.append(f"- {t.get('metric', '?')}: {t.get('direction', '?')} (significance: {t.get('significance', '?')})")
        lines.append("")

    recs = research_response.get("recommendations", [])
    if recs:
        lines.append("## Recommendations")
        for r in recs:
            lines.append(f"- [{r.get('priority', '?')}] {r.get('action', '?')}: {r.get('rationale', '?')} (timeline: {r.get('timeline', '?')})")
        lines.append("")

    if research_response.get("retrain"):
        lines.append(f"## Retraining Triggered")
        lines.append(f"Reason: {research_response.get('retrain_reason', 'unspecified')}")
        lines.append("")

    return "\n".join(lines)
