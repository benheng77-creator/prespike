// Stub created 2026-04-11 because the upstream golden-base snapshot omitted
// scripts/lib/. Real implementation enumerated build entry points and runtime
// dependencies for bundled plugins under extensions/. With that tree absent
// there is nothing to enumerate.

export function listBundledPluginBuildEntries() {
  return [];
}

export function listBundledPluginRuntimeDependencies() {
  return [];
}
