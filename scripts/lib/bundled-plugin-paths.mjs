// Stub created 2026-04-11 because the upstream golden-base snapshot omitted
// scripts/lib/. Real implementation enumerated bundled-plugin directories under
// extensions/. With the source tree absent there is nothing to enumerate, so
// this exposes the same shape with empty/zero defaults.

import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");

export const BUNDLED_PLUGIN_ROOT_DIR = "extensions";
export const bundledPluginRoot = path.join(repoRoot, BUNDLED_PLUGIN_ROOT_DIR);
export const BUNDLED_PLUGIN_PATH_PREFIX = `${BUNDLED_PLUGIN_ROOT_DIR}/`;

export function listBundledPluginDirectories() {
  return [];
}

export function listBundledPluginIds() {
  return [];
}
