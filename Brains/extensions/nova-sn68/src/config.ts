/**
 * Configuration resolution for nova-sn68 plugin.
 */

import type { NovaSn68Config } from "./types.js";

const DEFAULTS: NovaSn68Config = {
  novaDir: "/root/clawd/Nova",
  loopScript: "",
  sshHost: "swift-shark-ff",
  liumPod: "",
  commandTimeoutMs: 120_000,
};

/**
 * Merge plugin config with defaults.
 */
export function resolveConfig(
  pluginConfig?: Partial<NovaSn68Config>
): NovaSn68Config {
  const merged = { ...DEFAULTS };
  if (!pluginConfig) return merged;

  if (pluginConfig.novaDir) merged.novaDir = pluginConfig.novaDir;
  if (pluginConfig.loopScript) merged.loopScript = pluginConfig.loopScript;
  if (pluginConfig.sshHost) merged.sshHost = pluginConfig.sshHost;
  if (pluginConfig.liumPod) merged.liumPod = pluginConfig.liumPod;
  if (pluginConfig.commandTimeoutMs)
    merged.commandTimeoutMs = pluginConfig.commandTimeoutMs;

  return merged;
}
