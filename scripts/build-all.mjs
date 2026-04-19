// Stub created 2026-04-11. The upstream golden-base snapshot omitted the
// original scripts/build-all.mjs which orchestrated tsdown + several post-
// build steps (runtime-postbuild.mjs, build-stamp.mjs, copy-export-html-
// templates.ts, write-build-info.ts, etc). None of those scripts shipped in
// this snapshot and there is no application source to feed tsdown anyway.
//
// This thin reimplementation:
//   1. Runs `tsc -p tsconfig.plugin-sdk.dts.json` (the only typecheck path
//      that emits anything in this scaffold).
//   2. Reports a clear no-op message for the rest of the build pipeline so
//      operators understand why nothing else is produced.
//
// Replace this with the upstream runner once the source tree is restored.

import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..");
const isWindows = process.platform === "win32";

const tscBin = path.join(repoRoot, "node_modules", ".bin", isWindows ? "tsc.CMD" : "tsc");

console.log("[build-all] running plugin-sdk dts emit (tsconfig.plugin-sdk.dts.json)");
const dts = spawnSync(tscBin, ["-p", "tsconfig.plugin-sdk.dts.json"], {
  cwd: repoRoot,
  stdio: "inherit",
  shell: isWindows,
});

if ((dts.status ?? 1) !== 0) {
  console.error("[build-all] dts emit failed");
  process.exit(dts.status ?? 1);
}

console.log("[build-all] tsdown bundle skipped: no application source present in this snapshot.");
console.log(
  "[build-all] runtime-postbuild, copy-export-html-templates, write-build-info, build-stamp:",
);
console.log("[build-all]   skipped — original orchestration scripts not in snapshot.");
console.log("[build-all] done.");
process.exit(0);
