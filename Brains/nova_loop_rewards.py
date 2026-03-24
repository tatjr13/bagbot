"""Reward tracking using the actual SN68 validator scoring path.

SN68 validator scoring (public validator code):
  - Winner selection based on rounded boltz_score
  - Tiebreaker: block_submitted (earlier wins), then push_time
  - Weight setting: 0.722 burned, remaining weight to winner_boltz
  - One winner per target per tempo

Strategy implications:
  - Maximize boltz_score (the primary discriminator)
  - Submit early only as a tiebreaker advantage
  - Replacing a submission only makes sense if boltz_score improves
    (block_submitted resets, so you lose tiebreaker advantage)

Attribution windows align to tempo boundaries (~360 blocks / ~72 min)
because rewards are distributed at tempo end.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from Brains.nova_loop_ssh import PodSSH
from Brains.nova_loop_state import NovaStateDB, iso_now

log = logging.getLogger(__name__)


def parse_scores_from_output(raw: str) -> dict[str, Any]:
    """Parse miner score output into structured data.

    The miner writes a JSON status file or emits parseable log output.
    We look for boltz_score (the validator's primary criterion), plus
    block_submitted and rank as secondary info.
    """
    # Try JSON first
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Regex fallback for common log patterns
    result: dict[str, Any] = {}

    # boltz_score is the primary validator metric
    boltz_match = re.search(r"boltz_score\s*[=:]\s*([-\d.]+)", raw, re.IGNORECASE)
    if boltz_match:
        result["boltz_score"] = float(boltz_match.group(1))

    # Also capture final_score if present (validator's combined metric)
    final_match = re.search(r"final_score\s*[=:]\s*([-\d.]+)", raw, re.IGNORECASE)
    if final_match:
        result["final_score"] = float(final_match.group(1))

    # block_submitted — tiebreaker (lower = earlier = better)
    block_match = re.search(r"block_submitted\s*[=:]\s*(\d+)", raw, re.IGNORECASE)
    if block_match:
        result["block_submitted"] = int(block_match.group(1))

    # Fallback: generic "score" pattern
    if "boltz_score" not in result:
        score_match = re.search(r"(?:score)\s*[=:]\s*([-\d.]+)", raw, re.IGNORECASE)
        if score_match:
            result["boltz_score"] = float(score_match.group(1))

    rank_match = re.search(r"rank\s*[=:]\s*(\d+)", raw, re.IGNORECASE)
    if rank_match:
        result["rank"] = int(rank_match.group(1))

    field_match = re.search(r"(?:field_size|total_miners|n_miners)\s*[=:]\s*(\d+)", raw, re.IGNORECASE)
    if field_match:
        result["field_size"] = int(field_match.group(1))

    return result


def fetch_current_scores(ssh: PodSSH) -> dict[str, Any]:
    """Read current scores from the GPU pod."""
    # Try JSON status file first
    raw = ssh.read_file("/tmp/nova_scores.json")
    if raw:
        scores = parse_scores_from_output(raw)
        if scores:
            return scores

    # Try miner log
    result = ssh.run(
        "tail -100 /tmp/nova_miner.log 2>/dev/null | "
        "grep -i 'boltz_score\\|final_score\\|block_submitted\\|rank' | tail -10",
        timeout=10,
    )
    if result.ok and result.stdout:
        scores = parse_scores_from_output(result.stdout)
        if scores:
            return scores

    return {}


def compute_reward_snapshot(
    scores: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a reward snapshot using the validator's actual scoring path.

    Primary metric: boltz_score (rounded by validator for winner selection)
    Secondary: block_submitted (tiebreaker — lower is better)
    """
    boltz_score = scores.get("boltz_score")
    final_score = scores.get("final_score")
    leader_score = scores.get("leader_score")
    block_submitted = scores.get("block_submitted")
    rank = scores.get("rank")
    field_size = scores.get("field_size")

    # Use boltz_score as the primary "our_score" metric.
    # Can't use `or` here — boltz_score == 0.0 is falsy but valid.
    our_score = boltz_score if boltz_score is not None else final_score

    score_gap = None
    if our_score is not None and leader_score is not None:
        score_gap = our_score - leader_score

    # Local improvement check
    local_improvement = None
    if previous and our_score is not None:
        prev_score = previous.get("our_score")
        if prev_score is not None:
            local_improvement = our_score - prev_score

    return {
        "our_score": our_score,          # boltz_score (primary validator criterion)
        "leader_score": leader_score,
        "score_gap": score_gap,
        "rank": rank,
        "field_size": field_size,
        "heavy_norm": None,              # deprecated — use boltz_score
        "block_submitted": block_submitted,
        "local_improvement": local_improvement,
    }


def update_rewards(db: NovaStateDB, ssh: PodSSH) -> dict[str, Any] | None:
    """Fetch scores and record a reward snapshot. Returns the snapshot or None."""
    scores = fetch_current_scores(ssh)
    if not scores:
        log.debug("No scores available from GPU pod")
        return None

    previous = db.latest_reward_snapshot()
    snapshot = compute_reward_snapshot(scores, previous)

    db.record_reward_snapshot(
        our_score=snapshot.get("our_score"),
        leader_score=snapshot.get("leader_score"),
        score_gap=snapshot.get("score_gap"),
        rank=snapshot.get("rank"),
        field_size=snapshot.get("field_size"),
        heavy_norm=None,
        metadata={
            "local_improvement": snapshot.get("local_improvement"),
            "block_submitted": snapshot.get("block_submitted"),
            "raw_scores": scores,
        },
    )

    if snapshot.get("our_score") is not None:
        db.record_metric("boltz_score", snapshot["our_score"])
    if snapshot.get("score_gap") is not None:
        db.record_metric("score_gap", snapshot["score_gap"])
    if snapshot.get("rank") is not None:
        db.record_metric("rank", float(snapshot["rank"]))

    return snapshot


def should_replace_submission(
    current_boltz_score: float,
    new_boltz_score: float,
    current_block_submitted: int | None = None,
    current_block: int | None = None,
) -> tuple[bool, str]:
    """Decide if a new candidate should replace the current submission.

    Validator scoring path:
      1. Winner = highest rounded boltz_score
      2. Tiebreaker = earliest block_submitted (then push_time)

    Replacing costs tiebreaker advantage (block_submitted resets to now).
    Only replace if the boltz_score actually improves when rounded.

    Returns (should_replace, reason).
    """
    # Public validator rounds boltz_score to 4 decimal places,
    # then compares for winner selection.
    current_rounded = round(current_boltz_score, 4)
    new_rounded = round(new_boltz_score, 4)

    if new_rounded <= current_rounded:
        return False, (
            f"no boltz_score improvement after rounding "
            f"(current={current_rounded}, new={new_rounded})"
        )

    # New score is strictly better after rounding — replace
    improvement = new_rounded - current_rounded
    return True, (
        f"boltz_score improves by {improvement:.4f} after rounding "
        f"(current={current_rounded}, new={new_rounded}). "
        f"Tiebreaker advantage lost but score improvement justifies replacement."
    )


def compute_surrogate_correlation(db: NovaStateDB, n_recent: int = 20) -> dict[str, Any]:
    """Compute how well the surrogate model correlates with actual Boltz2 scores."""
    proposals = db.recent_proposals(limit=n_recent)
    pairs = []
    for p in proposals:
        if p.get("score_before") is not None and p.get("score_after") is not None:
            pairs.append((p["score_before"], p["score_after"]))

    if len(pairs) < 3:
        return {"correlation": None, "n_samples": len(pairs), "sufficient": False}

    n = len(pairs)
    x_vals = [p[0] for p in pairs]
    y_vals = [p[1] for p in pairs]
    x_mean = sum(x_vals) / n
    y_mean = sum(y_vals) / n

    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    den_x = sum((x - x_mean) ** 2 for x in x_vals) ** 0.5
    den_y = sum((y - y_mean) ** 2 for y in y_vals) ** 0.5

    if den_x == 0 or den_y == 0:
        return {"correlation": 0.0, "n_samples": n, "sufficient": True}

    correlation = num / (den_x * den_y)
    return {"correlation": round(correlation, 4), "n_samples": n, "sufficient": True}
