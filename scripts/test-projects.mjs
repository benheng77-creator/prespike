// Stub created 2026-04-11. The upstream golden-base snapshot omitted the
// original scripts/test-projects.mjs which orchestrated ~50 vitest project
// configs (vitest.unit.config.ts, vitest.cli.config.ts, etc) under a custom
// shard runner. None of those project configs can resolve in this snapshot
// because they all chain through vitest.shared.config.ts and reference test
// files under src/, ui/, extensions/, and packages/ — directories which are
// absent.
//
// This thin reimplementation runs vitest at the repo root with
// `--passWithNoTests` so that `pnpm test` can complete cleanly when there is
// no application code to test. It does NOT silently report success in the
// presence of failing assertions: if any test file is found, vitest's normal
// pass/fail behavior takes over.
//
// Replace this with the upstream runner once the source tree is restored.

import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..");
const isWindows = process.platform === "win32";

const vitestBin = path.join(repoRoot, "node_modules", ".bin", isWindows ? "vitest.CMD" : "vitest");

console.log("[test-projects] no source tree present; running vitest --passWithNoTests");

const args = ["run", "--passWithNoTests", "--config", "vitest.config.placeholder.mjs"];
const result = spawnSync(vitestBin, args, {
  cwd: repoRoot,
  stdio: "inherit",
  shell: isWindows,
});

if (result.error) {
  console.error("[test-projects] failed to spawn vitest:", result.error.message);
  process.exit(1);
}

process.exit(result.status ?? 1);
