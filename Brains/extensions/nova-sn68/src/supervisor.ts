/**
 * Process supervisor for the Nova mining loop.
 *
 * Manages start/stop/status of nova_mining_loop.py, persists PID,
 * and reads workspace files for status reporting.
 */

import { readFileSync, writeFileSync, existsSync } from "fs";
import { join } from "path";
import type { NovaSn68Config, WorkerStatus, DbSummary } from "./types.js";

interface RunCommand {
  (cmd: string, timeoutMs?: number): Promise<{ stdout: string; stderr: string; exitCode: number }>;
}

interface Logger {
  info(msg: string): void;
  warn(msg: string): void;
  error(msg: string): void;
}

interface WorkerState {
  pid: number;
  startedAt: string;
  loopScript: string;
}

export class Supervisor {
  private config: NovaSn68Config;
  private runCommand: RunCommand;
  private logger: Logger;

  constructor(config: NovaSn68Config, runCommand: RunCommand, logger: Logger) {
    this.config = config;
    this.runCommand = runCommand;
    this.logger = logger;
  }

  // ── start ──────────────────────────────────────────────────────────────

  async start(extraArgs: string[] = []): Promise<{ success: boolean; pid?: number; error?: string }> {
    // Check if already running
    const status = await this.getStatus();
    if (status.running) {
      return { success: false, error: `Loop already running (PID ${status.pid})` };
    }

    const script = this.config.loopScript || this.defaultLoopScript();
    if (!script) {
      return { success: false, error: "No loop script configured and default not found" };
    }

    const argsStr = extraArgs.length > 0 ? " " + extraArgs.join(" ") : "";
    const cmd = `nohup bash ${script}${argsStr} > ${this.loopLogPath()} 2>&1 & echo $!`;

    try {
      const result = await this.runCommand(cmd, this.config.commandTimeoutMs);
      const pid = parseInt(result.stdout.trim(), 10);

      if (isNaN(pid) || pid <= 0) {
        return { success: false, error: `Failed to get PID: ${result.stderr}` };
      }

      // Save worker state
      const state: WorkerState = {
        pid,
        startedAt: new Date().toISOString(),
        loopScript: script,
      };
      this.writeWorkerState(state);
      this.logger.info(`Nova loop started with PID ${pid}`);

      return { success: true, pid };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  }

  // ── stop ───────────────────────────────────────────────────────────────

  async stop(): Promise<{ success: boolean; error?: string }> {
    const state = this.readWorkerState();
    if (!state) {
      return { success: false, error: "No worker state found (loop may not be running)" };
    }

    try {
      // Graceful SIGTERM
      await this.runCommand(`kill ${state.pid} 2>/dev/null || true`, 5000);

      // Wait briefly for graceful shutdown
      await new Promise((resolve) => setTimeout(resolve, 2000));

      // Check if still alive
      const alive = await this.isProcessAlive(state.pid);
      if (alive) {
        // Force kill
        await this.runCommand(`kill -9 ${state.pid} 2>/dev/null || true`, 5000);
        this.logger.warn(`Force-killed nova loop PID ${state.pid}`);
      }

      this.clearWorkerState();
      this.logger.info(`Nova loop stopped (PID ${state.pid})`);
      return { success: true };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  }

  // ── status ─────────────────────────────────────────────────────────────

  async getStatus(): Promise<WorkerStatus> {
    const state = this.readWorkerState();
    let running = false;
    let pid: number | null = null;
    let uptimeSeconds: number | null = null;

    if (state) {
      running = await this.isProcessAlive(state.pid);
      if (running) {
        pid = state.pid;
        uptimeSeconds = Math.floor(
          (Date.now() - new Date(state.startedAt).getTime()) / 1000
        );
      } else {
        // Stale state — process died
        this.clearWorkerState();
      }
    }

    // Read STATUS.md
    const statusMarkdown = this.readFile(
      join(this.config.novaDir, "STATUS.md"),
      "Loop not running — no status available."
    );

    // Read loop state for pause info
    const loopState = this.readJsonFile(
      join(this.config.novaDir, ".loop_state.json")
    );

    return {
      running,
      pid,
      uptimeSeconds,
      paused: false,  // Pause state is in the Python process, not readable from here directly
      pauseReason: "",
      statusMarkdown,
      dbSummary: null,  // Would need sqlite3 binding to read — status MD is sufficient
    };
  }

  // ── directive ──────────────────────────────────────────────────────────

  async writeDirective(text: string): Promise<{ success: boolean }> {
    const inboxPath = join(this.config.novaDir, "INBOX.md");
    const existing = this.readFile(inboxPath, "");
    const entry = `- ${text}\n`;
    const content = existing ? existing.trimEnd() + "\n" + entry : entry;

    try {
      writeFileSync(inboxPath, content, "utf-8");
      this.logger.info(`Directive written to INBOX: ${text.substring(0, 80)}`);
      return { success: true };
    } catch {
      return { success: false };
    }
  }

  // ── telemetry ──────────────────────────────────────────────────────────

  readOutbox(): { items: string[]; hasUrgent: boolean; urgentItems: string[] } {
    const outboxPath = join(this.config.novaDir, "OUTBOX.md");
    const raw = this.readFile(outboxPath, "");
    if (!raw.trim()) {
      return { items: [], hasUrgent: false, urgentItems: [] };
    }

    const items: string[] = [];
    const urgentItems: string[] = [];

    for (const line of raw.split("\n")) {
      const trimmed = line.trim();
      if (trimmed.startsWith("- ")) {
        const item = trimmed.substring(2);
        items.push(item);
        if (item.includes("**") || item.toLowerCase().includes("urgent")) {
          urgentItems.push(item);
        }
      }
    }

    return { items, hasUrgent: urgentItems.length > 0, urgentItems };
  }

  clearOutbox(): void {
    const outboxPath = join(this.config.novaDir, "OUTBOX.md");
    try {
      writeFileSync(outboxPath, "", "utf-8");
    } catch {
      // Ignore
    }
  }

  readRecentLoopLog(lines: number = 30): string {
    const logPath = this.loopLogPath();
    try {
      const content = readFileSync(logPath, "utf-8");
      const allLines = content.split("\n");
      return allLines.slice(-lines).join("\n");
    } catch {
      return "No loop log available.";
    }
  }

  // ── internals ──────────────────────────────────────────────────────────

  private defaultLoopScript(): string {
    // Look for run_nova_loop.sh relative to the Brains directory
    const candidates = [
      join(this.config.novaDir, "..", "Brains", "run_nova_loop.sh"),
      "/root/clawd/Brains/run_nova_loop.sh",
      "/home/timt/Desktop/Active/TravisBot/bagbot/Brains/run_nova_loop.sh",
    ];
    for (const candidate of candidates) {
      if (existsSync(candidate)) return candidate;
    }
    return "";
  }

  private loopLogPath(): string {
    return join(this.config.novaDir, "LOOP.log");
  }

  private workerStatePath(): string {
    return join(this.config.novaDir, ".worker_state.json");
  }

  private readWorkerState(): WorkerState | null {
    try {
      const raw = readFileSync(this.workerStatePath(), "utf-8");
      return JSON.parse(raw) as WorkerState;
    } catch {
      return null;
    }
  }

  private writeWorkerState(state: WorkerState): void {
    try {
      writeFileSync(
        this.workerStatePath(),
        JSON.stringify(state, null, 2),
        "utf-8"
      );
    } catch (err) {
      this.logger.error(`Failed to write worker state: ${err}`);
    }
  }

  private clearWorkerState(): void {
    try {
      writeFileSync(this.workerStatePath(), "", "utf-8");
    } catch {
      // Ignore
    }
  }

  private async isProcessAlive(pid: number): Promise<boolean> {
    try {
      const result = await this.runCommand(
        `kill -0 ${pid} 2>/dev/null && echo alive || echo dead`,
        5000
      );
      return result.stdout.trim() === "alive";
    } catch {
      return false;
    }
  }

  private readFile(path: string, fallback: string): string {
    try {
      return readFileSync(path, "utf-8");
    } catch {
      return fallback;
    }
  }

  private readJsonFile(path: string): Record<string, unknown> | null {
    try {
      return JSON.parse(readFileSync(path, "utf-8"));
    } catch {
      return null;
    }
  }
}
