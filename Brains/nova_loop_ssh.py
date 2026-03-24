"""SSH wrapper for GPU pod operations.

Supports both raw SSH and lium-based connections.  Every command runs
through a single gateway so we can add retry/timeout/logging uniformly.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from Brains.nova_loop_config import SSHConfig

log = logging.getLogger(__name__)


@dataclass
class SSHResult:
    """Outcome of a single remote command."""
    stdout: str
    stderr: str
    returncode: int
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class PodSSH:
    """Thin wrapper around SSH / lium for GPU pod operations."""

    def __init__(self, cfg: SSHConfig) -> None:
        self.cfg = cfg
        self._last_ok: float = 0.0

    # ── core execution ──────────────────────────────────────────────────

    def run(self, cmd: str, *, timeout: int = 30) -> SSHResult:
        """Execute *cmd* on the pod and return the result."""
        if self.cfg.lium_pod:
            full_cmd = ["lium", "ssh", self.cfg.lium_pod, "--", "bash", "-lc", cmd]
        else:
            ssh_args = self._ssh_base_args()
            full_cmd = ssh_args + [cmd]

        started = time.perf_counter()
        try:
            proc = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            result = SSHResult(
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
                returncode=proc.returncode,
                elapsed_ms=elapsed,
            )
        except subprocess.TimeoutExpired:
            elapsed = int((time.perf_counter() - started) * 1000)
            result = SSHResult(
                stdout="",
                stderr=f"SSH command timed out after {timeout}s",
                returncode=-1,
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            result = SSHResult(
                stdout="",
                stderr=str(exc),
                returncode=-1,
                elapsed_ms=elapsed,
            )

        if result.ok:
            self._last_ok = time.time()
        else:
            log.warning("ssh cmd failed: %s | rc=%d | stderr=%s", cmd[:80], result.returncode, result.stderr[:200])

        return result

    # ── GPU health ──────────────────────────────────────────────────────

    def gpu_alive(self) -> dict[str, Any]:
        """Check if GPU is accessible and return basic info."""
        r = self.run("nvidia-smi --query-gpu=gpu_name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits", timeout=15)
        if not r.ok:
            return {"alive": False, "error": r.stderr}
        parts = [p.strip() for p in r.stdout.split(",")]
        if len(parts) < 5:
            return {"alive": True, "raw": r.stdout}
        return {
            "alive": True,
            "gpu_name": parts[0],
            "temp_c": _int_or(parts[1], -1),
            "util_pct": _int_or(parts[2], -1),
            "mem_used_mb": _int_or(parts[3], -1),
            "mem_total_mb": _int_or(parts[4], -1),
        }

    def gpu_temp(self) -> int:
        """Return GPU temperature in Celsius, -1 on failure."""
        info = self.gpu_alive()
        return info.get("temp_c", -1)

    # ── miner process ───────────────────────────────────────────────────

    def miner_alive(self) -> bool:
        """Check if the Nova miner process is running."""
        r = self.run("pgrep -f 'nova.*miner\\|neurons/miner'", timeout=10)
        return r.ok and bool(r.stdout.strip())

    def miner_pid(self) -> int | None:
        """Return miner PID or None."""
        r = self.run("pgrep -f 'nova.*miner\\|neurons/miner' | head -1", timeout=10)
        if r.ok and r.stdout.strip().isdigit():
            return int(r.stdout.strip())
        return None

    def restart_miner(self, start_cmd: str = "") -> SSHResult:
        """Kill existing miner and start a new one.

        If *start_cmd* is empty, just kills the miner (useful in tests).
        """
        self.run("pkill -f 'nova.*miner\\|neurons/miner' || true", timeout=10)
        time.sleep(2)
        if not start_cmd:
            return SSHResult(stdout="killed only", stderr="", returncode=0, elapsed_ms=0)
        return self.run(f"nohup {start_cmd} > /tmp/nova_miner.log 2>&1 &", timeout=15)

    # ── label / data flow ───────────────────────────────────────────────

    def label_count(self, label_dir: str = "/tmp/nova_labels") -> int:
        """Count label files in the pod's label directory."""
        r = self.run(f"ls -1 {shlex.quote(label_dir)} 2>/dev/null | wc -l", timeout=10)
        if r.ok:
            return _int_or(r.stdout.strip(), 0)
        return 0

    def recent_label_age_seconds(self, label_dir: str = "/tmp/nova_labels") -> int:
        """Seconds since newest label file was modified. -1 on failure."""
        r = self.run(
            f"find {shlex.quote(label_dir)} -type f -printf '%T@\\n' 2>/dev/null | sort -rn | head -1",
            timeout=10,
        )
        if r.ok and r.stdout.strip():
            try:
                ts = float(r.stdout.strip())
                return int(time.time() - ts)
            except ValueError:
                pass
        return -1

    # ── file transfer helpers ───────────────────────────────────────────

    def upload(self, local: str, remote: str, *, timeout: int = 60) -> SSHResult:
        """SCP a local file to the pod."""
        if self.cfg.lium_pod:
            cmd = ["lium", "scp", self.cfg.lium_pod, local, remote]
        else:
            cmd = ["scp"] + self._scp_opts() + [local, f"{self.cfg.user}@{self.cfg.host}:{remote}"]
        started = time.perf_counter()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            elapsed = int((time.perf_counter() - started) * 1000)
            return SSHResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode, elapsed_ms=elapsed)
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return SSHResult(stdout="", stderr=str(exc), returncode=-1, elapsed_ms=elapsed)

    def download(self, remote: str, local: str, *, timeout: int = 60) -> SSHResult:
        """SCP a remote file from the pod."""
        if self.cfg.lium_pod:
            cmd = ["lium", "scp", self.cfg.lium_pod, f":{remote}", local]
        else:
            cmd = ["scp"] + self._scp_opts() + [f"{self.cfg.user}@{self.cfg.host}:{remote}", local]
        started = time.perf_counter()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            elapsed = int((time.perf_counter() - started) * 1000)
            return SSHResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode, elapsed_ms=elapsed)
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return SSHResult(stdout="", stderr=str(exc), returncode=-1, elapsed_ms=elapsed)

    # ── score / submission queries ──────────────────────────────────────

    def read_file(self, path: str, *, timeout: int = 10) -> str:
        """Read a text file on the pod."""
        r = self.run(f"cat {shlex.quote(path)}", timeout=timeout)
        return r.stdout if r.ok else ""

    def disk_free(self) -> str:
        """Return df -h output for the pod root."""
        r = self.run("df -h / | tail -1", timeout=10)
        return r.stdout if r.ok else "unknown"

    # ── internals ───────────────────────────────────────────────────────

    def _ssh_base_args(self) -> list[str]:
        args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"ConnectTimeout={self.cfg.connect_timeout}",
            "-p", str(self.cfg.port),
        ]
        if self.cfg.key_path:
            args += ["-i", self.cfg.key_path]
        args.append(f"{self.cfg.user}@{self.cfg.host}")
        return args

    def _scp_opts(self) -> list[str]:
        opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", f"ConnectTimeout={self.cfg.connect_timeout}",
            "-P", str(self.cfg.port),
        ]
        if self.cfg.key_path:
            opts += ["-i", self.cfg.key_path]
        return opts


def _int_or(s: str, default: int) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return default
