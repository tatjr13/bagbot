/**
 * Nova SN68 mining agent plugin for OpenClaw.
 *
 * SDK contract (current docs):
 *   - Import definePluginEntry from openclaw/plugin-sdk/plugin-entry
 *   - Tools use async execute(_id, params)
 *   - Services use { id, start, stop } shape
 *   - Ship openclaw.plugin.json manifest
 *   - 5 tools, 1 background service, 1 hook
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { resolveConfig } from "./config.js";
import { Supervisor } from "./supervisor.js";
import type { NovaSn68Config } from "./types.js";

// PluginApi shape — typed inline until openclaw ships @types
interface PluginApi {
  pluginConfig?: Partial<NovaSn68Config>;
  logger: {
    info(msg: string): void;
    warn(msg: string): void;
    error(msg: string): void;
  };
  runtime: {
    system: {
      runCommandWithTimeout(
        cmd: string,
        timeoutMs?: number
      ): Promise<{ stdout: string; stderr: string; exitCode: number }>;
    };
  };
  registerTool(tool: {
    name: string;
    description: string;
    parameters?: Record<string, unknown>;
    execute: (id: string, params: Record<string, unknown>) => Promise<unknown>;
  }): void;
  registerService(service: {
    id: string;
    start: () => Promise<void>;
    stop: () => Promise<void>;
  }): void;
  on(
    event: string,
    handler: (
      context: Record<string, unknown>
    ) => Promise<
      | {
          prependContext?: string[];
          appendSystemContext?: string[];
        }
      | void
    >
  ): void;
}

export default definePluginEntry({
  id: "nova-sn68",
  name: "Nova SN68 Miner",
  description: "Supervise the Nova SN68 mining agent loop.",

  register(api: PluginApi) {
    const config = resolveConfig(api.pluginConfig);
    const supervisor = new Supervisor(
      config,
      api.runtime.system.runCommandWithTimeout.bind(api.runtime.system),
      api.logger
    );

    // ── nova_start ───────────────────────────────────────────────

    api.registerTool({
      name: "nova_start",
      description:
        "Launch the Nova SN68 mining loop as a supervised background process. " +
        "Returns the PID on success.",
      parameters: {
        type: "object",
        properties: {
          extraArgs: {
            type: "array",
            items: { type: "string" },
            description:
              "Additional CLI arguments for run_nova_loop.sh " +
              "(e.g. ['--dry-run', '--strategy-seconds', '60'])",
          },
        },
      },
      async execute(_id, params) {
        const extraArgs = (params.extraArgs as string[]) || [];
        return supervisor.start(extraArgs);
      },
    });

    // ── nova_stop ────────────────────────────────────────────────

    api.registerTool({
      name: "nova_stop",
      description: "Stop the running Nova SN68 mining loop.",
      async execute(_id, _params) {
        return supervisor.stop();
      },
    });

    // ── nova_status ──────────────────────────────────────────────

    api.registerTool({
      name: "nova_status",
      description:
        "Get structured status for the Nova SN68 mining loop. " +
        "Includes chain timing, GPU health, miner state, GitHub status, scores, " +
        "proposals, and safety state.",
      async execute(_id, _params) {
        const status = await supervisor.getStatus();
        return {
          running: status.running,
          pid: status.pid,
          uptimeSeconds: status.uptimeSeconds,
          paused: status.paused,
          pauseReason: status.pauseReason,
          statusMarkdown: status.statusMarkdown,
        };
      },
    });

    // ── nova_directive ───────────────────────────────────────────

    api.registerTool({
      name: "nova_directive",
      description:
        "Write a directive to the Nova mining loop. " +
        'Supports "FREEZE", "RESUME", or free-text strategy hints. ' +
        "The loop reads directives from INBOX.md on its next health cycle.",
      parameters: {
        type: "object",
        properties: {
          text: {
            type: "string",
            description: "The directive text to send to the mining loop",
          },
        },
        required: ["text"],
      },
      async execute(_id, params) {
        const text = params.text as string;
        if (!text?.trim()) {
          return { success: false, error: "Directive text cannot be empty" };
        }
        return supervisor.writeDirective(text.trim());
      },
    });

    // ── nova_telemetry ───────────────────────────────────────────

    api.registerTool({
      name: "nova_telemetry",
      description:
        "Read recent telemetry from the Nova mining loop. " +
        "Returns recent log lines and any outbox items.",
      parameters: {
        type: "object",
        properties: {
          lines: {
            type: "number",
            description:
              "Number of recent log lines to return (default: 30)",
          },
        },
      },
      async execute(_id, params) {
        const lines = (params.lines as number) || 30;
        const recentLog = supervisor.readRecentLoopLog(lines);
        const outbox = supervisor.readOutbox();
        return {
          recentLog,
          outboxItems: outbox.items,
          urgentItems: outbox.urgentItems,
        };
      },
    });

    // ── background service: OUTBOX relay ─────────────────────────
    // Uses the { id, start, stop } shape from the SDK.
    // start() sets up a polling interval; stop() tears it down.

    let outboxPollHandle: ReturnType<typeof setInterval> | null = null;

    api.registerService({
      id: "nova-outbox-relay",

      async start() {
        api.logger.info("nova-outbox-relay service starting (30s poll)");
        outboxPollHandle = setInterval(async () => {
          try {
            const outbox = supervisor.readOutbox();
            if (!outbox.hasUrgent) return;

            for (const item of outbox.urgentItems) {
              api.logger.warn(`[NOVA URGENT] ${item}`);
            }
            supervisor.clearOutbox();
          } catch (err) {
            api.logger.error(`outbox relay error: ${err}`);
          }
        }, 30_000);
      },

      async stop() {
        if (outboxPollHandle) {
          clearInterval(outboxPollHandle);
          outboxPollHandle = null;
        }
        api.logger.info("nova-outbox-relay service stopped");
      },
    });

    // ── hook: contextual injection during active turns ───────────

    api.on("before_prompt_build", async () => {
      const outbox = supervisor.readOutbox();
      if (!outbox.hasUrgent) return undefined;

      return {
        appendSystemContext: outbox.urgentItems.map(
          (item) => `[NOVA URGENT] ${item}`
        ),
      };
    });

    api.logger.info(
      `nova-sn68 plugin registered | novaDir=${config.novaDir}`
    );
  },
});
