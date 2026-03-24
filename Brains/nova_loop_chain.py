"""Chain-aware timing for the Nova mining loop.

Queries Bittensor subtensor for block, epoch, and tempo state.
Strategy fires based on epoch position (submission window), not wall clock.
Attribution matures on tempo boundaries, not fixed minute windows.

SN68 specifics:
- Blocks are ~12 seconds
- Submission window: ≤20 blocks before epoch end
- Tempo: ~360 blocks (~72 min) — rewards distributed at tempo boundary
- Strategy should evaluate during submission window, not on a fixed timer
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from Brains.nova_loop_ssh import PodSSH

log = logging.getLogger(__name__)

NETUID_SN68 = 68
BLOCK_TIME_SECONDS = 12
DEFAULT_TEMPO = 360           # ~72 min
DEFAULT_EPOCH_LENGTH = 361    # tempo + 1 (matches public validator: winner_block // 361)
SUBMISSION_WINDOW_BLOCKS = 20 # submit when ≤20 blocks left in epoch


@dataclass
class ChainState:
    """Snapshot of on-chain timing state."""
    current_block: int = 0
    tempo: int = DEFAULT_TEMPO
    epoch_length: int = DEFAULT_EPOCH_LENGTH
    blocks_into_epoch: int = 0
    blocks_until_epoch_end: int = 0
    blocks_into_tempo: int = 0
    blocks_until_tempo_end: int = 0
    in_submission_window: bool = False
    at_tempo_boundary: bool = False
    fetched_at: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.current_block > 0 and not self.error

    @property
    def epoch_progress_pct(self) -> float:
        if self.epoch_length <= 0:
            return 0.0
        return (self.blocks_into_epoch / self.epoch_length) * 100

    @property
    def tempo_progress_pct(self) -> float:
        if self.tempo <= 0:
            return 0.0
        return (self.blocks_into_tempo / self.tempo) * 100

    @property
    def seconds_until_epoch_end(self) -> int:
        return self.blocks_until_epoch_end * BLOCK_TIME_SECONDS

    @property
    def seconds_until_tempo_end(self) -> int:
        return self.blocks_until_tempo_end * BLOCK_TIME_SECONDS


@dataclass
class ChainAwareScheduler:
    """Decides when strategy and research timers should fire based on chain state.

    Health timer stays wall-clock (deterministic, no chain dependency).
    Strategy fires when in the submission window (≤20 blocks before epoch end).
    Research fires at tempo boundaries or on a wall-clock fallback.
    """

    # Config
    submission_window_blocks: int = SUBMISSION_WINDOW_BLOCKS
    min_blocks_between_strategy: int = 5  # rate-limit within submission window
    tempo_boundary_tolerance_blocks: int = 5  # fire research within N blocks after boundary
    strategy_fallback_seconds: float = 300.0  # 5 min fallback if chain unavailable
    research_fallback_seconds: float = 7200.0 # 2h fallback

    # Tracking
    last_strategy_block: int = 0
    last_strategy_time: float = 0.0
    last_research_block: int = 0
    last_research_time: float = 0.0
    last_research_tempo_number: int = -1  # which tempo we last ran research in

    # Last chain state for status rendering
    last_chain_state: ChainState = field(default_factory=ChainState)

    def should_run_strategy(self, chain: ChainState) -> tuple[bool, str]:
        """Check if strategy cycle should fire.

        The public miner searches continuously and re-submits within the
        last 20 blocks when the candidate improves.  We mirror that by
        allowing *multiple* strategy runs while inside the submission
        window, rate-limited to one run per ``min_blocks_between_runs``
        blocks so we don't thrash.

        Fallback: wall-clock if chain state is unavailable.
        """
        self.last_chain_state = chain
        now = time.time()

        if not chain.ok:
            # Chain unavailable — fall back to wall clock
            if now - self.last_strategy_time >= self.strategy_fallback_seconds:
                return True, "wall-clock fallback (chain unavailable)"
            return False, "waiting (chain unavailable, fallback not due)"

        # In submission window? Allow repeated runs, rate-limited by block gap.
        if chain.in_submission_window:
            blocks_since_last = chain.current_block - self.last_strategy_block
            if blocks_since_last >= self.min_blocks_between_strategy:
                return True, f"submission window ({chain.blocks_until_epoch_end} blocks left)"
            return False, (
                f"submission window but rate-limited "
                f"({blocks_since_last}/{self.min_blocks_between_strategy} blocks since last)"
            )

        return False, f"not in submission window ({chain.blocks_until_epoch_end} blocks to go)"

    def record_strategy_run(self, chain: ChainState) -> None:
        self.last_strategy_block = chain.current_block
        self.last_strategy_time = time.time()

    def should_run_research(self, chain: ChainState) -> tuple[bool, str]:
        """Check if research cycle should fire.

        Primary: fire AFTER a tempo boundary (rewards just distributed),
        specifically in the first ``tempo_boundary_tolerance_blocks``
        blocks of a new tempo.  We track by tempo number so we fire
        exactly once per boundary.

        Fallback: wall-clock if chain unavailable or no boundary in a while.
        """
        self.last_chain_state = chain
        now = time.time()

        if not chain.ok:
            if now - self.last_research_time >= self.research_fallback_seconds:
                return True, "wall-clock fallback (chain unavailable)"
            return False, "waiting (chain unavailable, fallback not due)"

        # Compute which tempo number we are in (monotonically increasing)
        current_tempo_number = chain.current_block // chain.tempo if chain.tempo > 0 else 0

        # Just crossed a tempo boundary? (first N blocks of new tempo)
        if (chain.blocks_into_tempo <= self.tempo_boundary_tolerance_blocks
                and current_tempo_number != self.last_research_tempo_number):
            return True, f"tempo boundary (tempo #{current_tempo_number}, block {chain.current_block})"

        # Fallback: haven't researched in a while
        if now - self.last_research_time >= self.research_fallback_seconds:
            return True, "wall-clock fallback (no recent tempo boundary)"

        return False, f"waiting for tempo boundary ({chain.blocks_until_tempo_end} blocks)"

    def record_research_run(self, chain: ChainState) -> None:
        self.last_research_block = chain.current_block
        self.last_research_time = time.time()
        if chain.ok and chain.tempo > 0:
            self.last_research_tempo_number = chain.current_block // chain.tempo

    def status_dict(self) -> dict[str, Any]:
        c = self.last_chain_state
        return {
            "current_block": c.current_block,
            "epoch_progress": f"{c.blocks_into_epoch}/{c.epoch_length} ({c.epoch_progress_pct:.0f}%)",
            "tempo_progress": f"{c.blocks_into_tempo}/{c.tempo} ({c.tempo_progress_pct:.0f}%)",
            "in_submission_window": c.in_submission_window,
            "blocks_until_epoch_end": c.blocks_until_epoch_end,
            "blocks_until_tempo_end": c.blocks_until_tempo_end,
            "last_strategy_block": self.last_strategy_block,
            "last_research_block": self.last_research_block,
            "chain_ok": c.ok,
            "chain_error": c.error,
        }


def fetch_chain_state(ssh: PodSSH, netuid: int = NETUID_SN68) -> ChainState:
    """Query chain state from the GPU pod (where bittensor SDK is installed).

    Runs a small Python snippet on the pod to get block/tempo/epoch info.
    This keeps the bittensor dependency on the pod, not on the control VPS.
    """
    # Python one-liner to query chain state — runs on the pod
    query_script = f"""python3 -c "
import json, bittensor as bt
sub = bt.Subtensor('finney')
block = int(sub.block)
tempo = int(sub.query_module('SubtensorModule', 'Tempo', [{netuid}]))
epoch_length = tempo + 1  # public validator uses tempo+1 (winner_block // 361)
blocks_into_epoch = block % epoch_length
blocks_until_epoch_end = epoch_length - blocks_into_epoch
blocks_into_tempo = block % tempo
blocks_until_tempo_end = tempo - blocks_into_tempo
print(json.dumps({{
    'block': block, 'tempo': tempo, 'epoch_length': epoch_length,
    'blocks_into_epoch': blocks_into_epoch,
    'blocks_until_epoch_end': blocks_until_epoch_end,
    'blocks_into_tempo': blocks_into_tempo,
    'blocks_until_tempo_end': blocks_until_tempo_end,
}}))
" 2>/dev/null
"""
    result = ssh.run(query_script, timeout=20)
    now = time.time()

    if not result.ok:
        log.warning("Chain state query failed: %s", result.stderr[:200])
        return ChainState(error=result.stderr[:200], fetched_at=now)

    try:
        import json
        data = json.loads(result.stdout.strip())
        blocks_until_epoch_end = data.get("blocks_until_epoch_end", 0)
        blocks_until_tempo_end = data.get("blocks_until_tempo_end", 0)

        return ChainState(
            current_block=data["block"],
            tempo=data["tempo"],
            epoch_length=data["epoch_length"],
            blocks_into_epoch=data["blocks_into_epoch"],
            blocks_until_epoch_end=blocks_until_epoch_end,
            blocks_into_tempo=data["blocks_into_tempo"],
            blocks_until_tempo_end=blocks_until_tempo_end,
            in_submission_window=blocks_until_epoch_end <= SUBMISSION_WINDOW_BLOCKS,
            at_tempo_boundary=data["blocks_into_tempo"] <= 5,  # just past boundary
            fetched_at=now,
        )
    except Exception as exc:
        log.warning("Chain state parse failed: %s (raw: %s)", exc, result.stdout[:200])
        return ChainState(error=str(exc), fetched_at=now)


def compute_attribution_window(chain: ChainState) -> int:
    """Compute attribution window in minutes aligned to tempo boundaries.

    Instead of a fixed 30-min window, we wait until the next tempo boundary
    (when rewards are actually distributed) to evaluate outcomes.
    """
    if not chain.ok:
        return 72  # fallback: one full tempo (~72 min)

    seconds_to_tempo = chain.blocks_until_tempo_end * BLOCK_TIME_SECONDS
    # Add one extra tempo to ensure we capture the reward distribution
    minutes = (seconds_to_tempo // 60) + (chain.tempo * BLOCK_TIME_SECONDS // 60)
    return max(int(minutes), 30)  # floor at 30 min
