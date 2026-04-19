import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

const readText = (p) => readFileSync(p, "utf8").replace(/^\uFEFF/, "");
const fail = (msg) => { console.error("fork-doctor:", msg); process.exit(1); };

const requiredNode =
  existsSync(".nvmrc") ? readText(".nvmrc").trim() :
  existsSync(".node-version") ? readText(".node-version").trim() :
  null;

if (requiredNode && process.versions.node !== requiredNode) {
  fail(`Node mismatch: current=${process.versions.node} required=${requiredNode}`);
}

let pkg;
try { pkg = JSON.parse(readText("package.json")); }
catch (e) { fail(`package.json invalid JSON: ${e.message}`); }

if (pkg.name !== "claw247-trading") {
  fail(`package.json name must be "claw247-trading", got "${pkg.name}"`);
}

const FORBIDDEN_PATHS = [
  "Dockerfile",
  "docker-compose.yml",
  "openclaw.mjs",
  "tsconfig.json",
  "tsconfig.oxlint.json",
  "tsconfig.plugin-sdk.dts.json",
  "tsdown.config.ts",
  "knip.config.ts",
  ".oxlintrc.json",
  ".oxfmtrc.jsonc",
  ".pre-commit-config.yaml",
  ".jscpd.json",
  "openclaw_layer",
  "src/plugin-sdk",
  "src/video-generation",
];
const reappeared = FORBIDDEN_PATHS.filter((p) => existsSync(p));
if (reappeared.length) {
  fail(`upstream paths re-appeared (should stay deleted): ${reappeared.join(", ")}`);
}

if (existsSync("vitest.config.ts") || existsSync("vitest.unit.config.ts")) {
  fail("root vitest configs re-appeared; this fork has no src/ tree to test");
}

const st = spawnSync("git", ["status", "--short"], { encoding: "utf8" });
if (st.status !== 0) { fail(st.stderr || st.stdout); }
const deleted = st.stdout.split(/\r?\n/).filter((l) => l.startsWith(" D "));
if (deleted.length) {
  console.error("tracked files deleted in worktree:");
  for (const l of deleted) console.error("  " + l);
  process.exit(1);
}

console.log(`fork-doctor ok | node ${process.versions.node} | pkg ${pkg.name}`);
