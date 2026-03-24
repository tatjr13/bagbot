/**
 * Shared TypeScript interfaces for the nova-sn68 plugin.
 */

export interface NovaSn68Config {
  /** Path to the Nova workspace directory */
  novaDir: string;
  /** Path to run_nova_loop.sh or nova_mining_loop.py */
  loopScript: string;
  /** SSH host for the GPU pod */
  sshHost: string;
  /** Lium pod ID if using lium SSH */
  liumPod: string;
  /** Timeout for shell commands in milliseconds */
  commandTimeoutMs: number;
}

export interface WorkerStatus {
  /** Whether the loop process is running */
  running: boolean;
  /** PID of the loop process, if running */
  pid: number | null;
  /** How long the loop has been running */
  uptimeSeconds: number | null;
  /** Whether the safety gate is paused */
  paused: boolean;
  /** Reason for pause, if paused */
  pauseReason: string;
  /** Contents of STATUS.md (last health/strategy update) */
  statusMarkdown: string;
  /** Summary from SQLite state DB */
  dbSummary: DbSummary | null;
}

export interface DbSummary {
  /** Proposal counts by status */
  proposals: Record<string, number>;
  /** Latest reward snapshot */
  latestReward: RewardSnapshot | null;
  /** Count of pending directives */
  pendingDirectives: number;
  /** Most recent events */
  recentEvents: NovaEvent[];
}

export interface RewardSnapshot {
  recorded_at: string;
  our_score: number | null;
  leader_score: number | null;
  score_gap: number | null;
  rank: number | null;
  field_size: number | null;
  heavy_norm: number | null;
}

export interface NovaEvent {
  timestamp: string;
  category: string;
  level: string;
  message: string;
}

export interface OutboxItem {
  timestamp: string;
  category: string;
  message: string;
  urgent: boolean;
}
