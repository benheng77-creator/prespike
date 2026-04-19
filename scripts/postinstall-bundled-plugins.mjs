import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const root = process.cwd();
const candidates = [
  path.join(root, "bundled-plugins.json"),
  path.join(root, "config", "bundled-plugins.json"),
  path.join(root, "scripts", "bundled-plugins.json"),
  path.join(root, "assets", "bundled-plugins.json"),
];

const manifestPath = candidates.find((p) => fs.existsSync(p));

if (!manifestPath) {
  console.log("[postinstall-bundled-plugins] no bundled plugin manifest found; skipping");
  process.exit(0);
}

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
} catch {
  console.log(
    `[postinstall-bundled-plugins] invalid manifest at ${path.relative(root, manifestPath)}; skipping`,
  );
  process.exit(0);
}

const plugins = Array.isArray(manifest)
  ? manifest
  : Array.isArray(manifest.plugins)
    ? manifest.plugins
    : [];

console.log(
  `[postinstall-bundled-plugins] manifest found at ${path.relative(root, manifestPath)} with ${plugins.length} entr${plugins.length === 1 ? "y" : "ies"}; compatibility shim completed`,
);
process.exit(0);
