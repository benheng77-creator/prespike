// Stub created 2026-04-11. The upstream golden-base snapshot omitted the
// original scripts/run-oxlint.mjs which wrapped oxlint with project-specific
// shard handling, fix-mode, and ignore-pattern resolution. This thin
// reimplementation only forwards arguments to the locally installed oxlint
// binary so that `pnpm lint` and `pnpm lint:fix` resolve. If the upstream
// runner is restored later, replace this file with the real one.

import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..");

const isWindows = process.platform === "win32";
const oxlintBin = path.join(repoRoot, "node_modules", ".bin", isWindows ? "oxlint.CMD" : "oxlint");

const passthrough = process.argv.slice(2);
const args = ["--config", path.join(repoRoot, ".oxlintrc.json"), ...passthrough, "."];

const result = spawnSync(oxlintBin, args, {
  cwd: repoRoot,
  stdio: "inherit",
  shell: isWindows,
});

if (result.error) {
  console.error("[run-oxlint] failed to spawn oxlint:", result.error.message);
  process.exit(1);
}

process.exit(result.status ?? 1);
