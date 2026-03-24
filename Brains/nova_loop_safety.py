"""Safety constraints, budgets, cooldowns, and circuit breakers.

Every action the loop wants to take passes through the SafetyGate before
execution.  Limits are tracked in the state DB so they survive restarts.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from Brains.nova_loop_config import SafetyConfig
from Brains.nova_loop_state import NovaStateDB

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SafetyGate:
    """Enforces operational safety limits for the mining loop."""

    def __init__(self, cfg: SafetyConfig, db: NovaStateDB) -> None:
        self.cfg = cfg
        self.db = db
        self._restart_timestamps: list[float] = []
        self._switch_timestamps: list[float] = []
        self._cooldowns: dict[str, float] = {}  # action -> earliest_allowed_time
        self._paused: bool = False
        self._pause_reason: str = ""

    # ── pause / resume ──────────────────────────────────────────────────

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    def pause(self, reason: str) -> None:
        self._paused = True
        self._pause_reason = reason
        self.db.log_event("safety", f"PAUSED: {reason}", level="urgent")
        log.warning("Safety PAUSE: %s", reason)

    def resume(self) -> None:
        self._paused = False
        self._pause_reason = ""
        self.db.log_event("safety", "RESUMED", level="info")
        log.info("Safety RESUMED")

    # ── restart limiter ─────────────────────────────────────────────────

    def can_restart_miner(self) -> tuple[bool, str]:
        """Check if a miner restart is within budget."""
        if self._paused:
            return False, f"paused: {self._pause_reason}"

        now = time.time()
        cutoff = now - 3600  # 1 hour window
        self._restart_timestamps = [t for t in self._restart_timestamps if t > cutoff]

        if len(self._restart_timestamps) >= self.cfg.max_restarts_per_hour:
            reason = f"restart budget exhausted ({self.cfg.max_restarts_per_hour}/hour)"
            self.db.log_event("safety", reason, level="warn")
            return False, reason

        return True, "ok"

    def record_restart(self) -> None:
        self._restart_timestamps.append(time.time())
        self.db.log_event("safety", "miner restart recorded", level="info")

    # ── target switch limiter ───────────────────────────────────────────

    def can_switch_target(self) -> tuple[bool, str]:
        """Check if a target switch is within budget."""
        if self._paused:
            return False, f"paused: {self._pause_reason}"

        now = time.time()
        cutoff = now - 86400  # 24h window
        self._switch_timestamps = [t for t in self._switch_timestamps if t > cutoff]

        if len(self._switch_timestamps) >= self.cfg.max_target_switches_per_day:
            reason = f"target switch budget exhausted ({self.cfg.max_target_switches_per_day}/day)"
            self.db.log_event("safety", reason, level="warn")
            return False, reason

        return True, "ok"

    def record_target_switch(self) -> None:
        self._switch_timestamps.append(time.time())
        self.db.log_event("safety", "target switch recorded", level="info")

    # ── cooldown tracking ───────────────────────────────────────────────

    def is_cooled_down(self, action: str) -> tuple[bool, str]:
        """Check if an action has passed its cooldown period."""
        if self._paused:
            return False, f"paused: {self._pause_reason}"

        earliest = self._cooldowns.get(action, 0.0)
        now = time.time()
        if now < earliest:
            remaining = int(earliest - now)
            return False, f"cooldown active for '{action}': {remaining}s remaining"
        return True, "ok"

    def set_cooldown(self, action: str, minutes: int | None = None) -> None:
        """Set a cooldown period after a failed action."""
        duration = (minutes or self.cfg.failed_action_cooldown_min) * 60
        self._cooldowns[action] = time.time() + duration
        self.db.log_event(
            "safety",
            f"cooldown set for '{action}': {minutes or self.cfg.failed_action_cooldown_min} min",
            level="info",
        )

    def clear_cooldown(self, action: str) -> None:
        self._cooldowns.pop(action, None)

    # ── parity gate ─────────────────────────────────────────────────────

    def can_deploy_model(self, parity_passed: bool) -> tuple[bool, str]:
        """Check if a model deploy is allowed."""
        if self._paused:
            return False, f"paused: {self._pause_reason}"

        if self.cfg.require_parity_for_deploy and not parity_passed:
            reason = "model deploy blocked: parity check not passed"
            self.db.log_event("safety", reason, level="warn")
            return False, reason

        return True, "ok"

    # ── composite check ─────────────────────────────────────────────────

    def check_action(self, action: str, **kwargs: Any) -> tuple[bool, str]:
        """Single entry point: check if an action is allowed.

        Returns (allowed, reason).
        """
        if self._paused:
            return False, f"paused: {self._pause_reason}"

        # Check cooldown
        ok, reason = self.is_cooled_down(action)
        if not ok:
            return False, reason

        # Action-specific checks
        if action == "restart_miner":
            return self.can_restart_miner()
        elif action == "switch_target":
            return self.can_switch_target()
        elif action == "deploy_model":
            parity = kwargs.get("parity_passed", False)
            return self.can_deploy_model(parity)

        return True, "ok"

    # ── directive handling ──────────────────────────────────────────────

    def handle_directive(self, directive_text: str) -> str | None:
        """Process safety-relevant directives. Returns action taken or None."""
        text = directive_text.strip().upper()

        if text in ("FREEZE", "PAUSE", "STOP"):
            self.pause(f"operator directive: {directive_text.strip()}")
            return "paused"

        if text in ("RESUME", "UNFREEZE", "GO"):
            self.resume()
            return "resumed"

        return None

    # ── status ──────────────────────────────────────────────────────────

    def status_dict(self) -> dict[str, Any]:
        """Snapshot of safety state for status rendering."""
        now = time.time()
        hour_restarts = len([t for t in self._restart_timestamps if t > now - 3600])
        day_switches = len([t for t in self._switch_timestamps if t > now - 86400])
        active_cooldowns = {
            action: int(earliest - now)
            for action, earliest in self._cooldowns.items()
            if earliest > now
        }
        return {
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "restarts_this_hour": hour_restarts,
            "max_restarts_per_hour": self.cfg.max_restarts_per_hour,
            "target_switches_today": day_switches,
            "max_switches_per_day": self.cfg.max_target_switches_per_day,
            "active_cooldowns": active_cooldowns,
        }
