// Stub created 2026-04-11 because the upstream golden-base snapshot omitted
// scripts/lib/. Real implementation enumerated plugin-SDK subpath exports
// declared in package.json and resolved their entry source files. With
// src/plugin-sdk/ absent there is nothing to enumerate, so this exposes the
// same shape with empty defaults.

export const pluginSdkSubpaths = [];

export function buildPluginSdkEntrySources() {
  return [];
}

export function listPluginSdkEntries() {
  return [];
}
