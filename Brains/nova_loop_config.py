"""Configuration for the Nova SN68 mining loop.

All tunables in one place.  CLI flags override defaults (see nova_mining_loop.py).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── workspace directory (the file-based interface) ──────────────────────────
NOVA_DIR_CANDIDATES = (
    Path("/root/clawd/Nova"),
    Path.home() / "clawd" / "Nova",
    ROOT / "Nova",
)

DEFAULT_CHUTES_BASE_URL = "https://llm.chutes.ai/v1/chat/completions"
# No default model IDs — must be set via CLI or env.
# Chutes model availability changes; hardcoding IDs causes silent failures.
# Verify availability: curl -H "Authorization: Bearer $CHUTES_API_KEY" \
#   https://llm.chutes.ai/v1/models | jq '.data[].id'
NO_MODEL = ""


def resolve_nova_dir() -> Path:
    override = os.environ.get("NOVA_DIR", "").strip()
    if override:
        return Path(override)
    for candidate in NOVA_DIR_CANDIDATES:
        if candidate.exists():
            return candidate
    return ROOT / "Nova"


@dataclass
class SSHConfig:
    """SSH connection to the GPU pod."""
    host: str = "swift-shark-ff"
    user: str = "root"
    port: int = 22
    key_path: str = ""
    connect_timeout: int = 10
    # lium pod ID (if using lium ssh instead of raw SSH)
    lium_pod: str = ""


@dataclass
class TimerConfig:
    """Cadences for the three loop timers."""
    cycle_seconds: float = 5.0       # base sleep
    health_seconds: float = 20.0     # GPU/miner health
    strategy_seconds: float = 300.0  # 5 min
    research_seconds: float = 7200.0 # 2 hours


@dataclass
class LLMConfig:
    """Chutes LLM provider settings.

    Model IDs have no defaults — they MUST be provided via CLI flags
    (--strategy-model, --research-model) or the launch script.
    Chutes model availability changes; verify with their /v1/models endpoint.
    """
    base_url: str = DEFAULT_CHUTES_BASE_URL
    api_key_env: str = "CHUTES_API_KEY"
    strategy_model: str = NO_MODEL     # REQUIRED — set via --strategy-model
    strategy_temperature: float = 0.7
    strategy_max_tokens: int = 2000
    research_model: str = NO_MODEL     # REQUIRED — set via --research-model
    research_temperature: float = 0.5
    research_max_tokens: int = 3000
    timeout: float = 90.0
    retries: int = 3
    budget_per_day: int = 5000
    stagnation_temp_boost: float = 0.2  # added to temp when stagnating


@dataclass
class SafetyConfig:
    """Circuit breaker limits."""
    max_restarts_per_hour: int = 3
    max_target_switches_per_day: int = 2
    failed_action_cooldown_min: int = 15
    require_parity_for_deploy: bool = True


@dataclass
class NovaConfig:
    """Top-level configuration for the Nova mining loop."""
    ssh: SSHConfig = field(default_factory=SSHConfig)
    timers: TimerConfig = field(default_factory=TimerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    # Workspace paths (file-based interface)
    nova_dir: Path = field(default_factory=resolve_nova_dir)

    # Flags
    dry_run: bool = False
    run_once: bool = False

    @property
    def status_path(self) -> Path:
        return self.nova_dir / "STATUS.md"

    @property
    def outbox_path(self) -> Path:
        return self.nova_dir / "OUTBOX.md"

    @property
    def inbox_path(self) -> Path:
        return self.nova_dir / "INBOX.md"

    @property
    def tasks_path(self) -> Path:
        return self.nova_dir / "TASKS.md"

    @property
    def briefing_path(self) -> Path:
        return self.nova_dir / "BRIEFING.md"

    @property
    def runs_dir(self) -> Path:
        return self.nova_dir / "RUNS"

    @property
    def db_path(self) -> Path:
        return self.nova_dir / ".state.db"

    @property
    def loop_state_path(self) -> Path:
        return self.nova_dir / ".loop_state.json"

    @property
    def loop_log_path(self) -> Path:
        return self.nova_dir / "LOOP.log"

    @property
    def api_key(self) -> str:
        return os.environ.get(self.llm.api_key_env, "").strip()

    def ensure_dirs(self) -> None:
        """Create workspace directories if they don't exist."""
        self.nova_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)


def parse_args() -> NovaConfig:
    """Build config from CLI args, following arbos_terminal_loop.py pattern."""
    parser = argparse.ArgumentParser(description="Run the Nova SN68 mining loop.")

    # Timer settings
    parser.add_argument("--cycle-seconds", type=float, default=5.0)
    parser.add_argument("--health-seconds", type=float, default=20.0)
    parser.add_argument("--strategy-seconds", type=float, default=300.0)
    parser.add_argument("--research-seconds", type=float, default=7200.0)

    # SSH
    parser.add_argument("--ssh-host", default="swift-shark-ff")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-key", default="")
    parser.add_argument("--lium-pod", default="")

    # LLM — model IDs are required, no safe defaults
    parser.add_argument("--strategy-model", default="",
                        help="Chutes model ID for strategy (e.g. 'Qwen/Qwen3-235B-A22B')")
    parser.add_argument("--research-model", default="",
                        help="Chutes model ID for research (e.g. 'deepseek-ai/DeepSeek-V3-0324')")
    parser.add_argument("--strategy-temp", type=float, default=0.7)
    parser.add_argument("--research-temp", type=float, default=0.5)
    parser.add_argument("--strategy-max-tokens", type=int, default=2000)
    parser.add_argument("--research-max-tokens", type=int, default=3000)
    parser.add_argument("--chutes-timeout", type=float, default=90.0)
    parser.add_argument("--chutes-retries", type=int, default=3)
    parser.add_argument("--budget-per-day", type=int, default=5000)
    parser.add_argument("--api-key-env", default="CHUTES_API_KEY")
    parser.add_argument("--base-url", default=DEFAULT_CHUTES_BASE_URL)

    # Safety
    parser.add_argument("--max-restarts-per-hour", type=int, default=3)
    parser.add_argument("--max-target-switches-per-day", type=int, default=2)

    # Workspace
    parser.add_argument("--nova-dir", default="")

    # Flags
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-once", action="store_true")

    args = parser.parse_args()

    nova_dir = Path(args.nova_dir) if args.nova_dir else resolve_nova_dir()

    return NovaConfig(
        ssh=SSHConfig(
            host=args.ssh_host,
            user=args.ssh_user,
            port=args.ssh_port,
            key_path=args.ssh_key,
            lium_pod=args.lium_pod,
        ),
        timers=TimerConfig(
            cycle_seconds=args.cycle_seconds,
            health_seconds=args.health_seconds,
            strategy_seconds=args.strategy_seconds,
            research_seconds=args.research_seconds,
        ),
        llm=LLMConfig(
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            strategy_model=args.strategy_model,
            strategy_temperature=args.strategy_temp,
            strategy_max_tokens=args.strategy_max_tokens,
            research_model=args.research_model,
            research_temperature=args.research_temp,
            research_max_tokens=args.research_max_tokens,
            timeout=args.chutes_timeout,
            retries=args.chutes_retries,
            budget_per_day=args.budget_per_day,
        ),
        safety=SafetyConfig(
            max_restarts_per_hour=args.max_restarts_per_hour,
            max_target_switches_per_day=args.max_target_switches_per_day,
        ),
        nova_dir=nova_dir,
        dry_run=args.dry_run,
        run_once=args.run_once,
    )
