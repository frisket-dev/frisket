import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const sdkRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = resolve(sdkRoot, '..');
const manifestPath = resolve(repoRoot, 'sdk', 'contract-manifest.json');
const outPath = resolve(sdkRoot, 'src', 'contracts.gen.ts');

const manifest = JSON.parse(readFileSync(manifestPath, 'utf-8'));

function requireKey(key) {
  if (!(key in manifest)) {
    throw new Error(`sdk/contract-manifest.json is missing required key: ${key}`);
  }
  return manifest[key];
}

const literal = (value) => JSON.stringify(value, null, 2);

const banner = `// GENERATED FILE — do not hand-edit.
// Produced by sdk/scripts/gen-contracts.mjs from sdk/contract-manifest.json
// (itself generated from the Python contract source —
// src/frisket/authoring/workbench/contracts.py, src/frisket/contracts/plugin.py — by
// \`uv run python scripts/ci/gen_contract_manifest.py\`). Regenerate this file
// with \`npm run build\`; it is not committed.
`;

const body = `
export const PLUGIN_LEGAL_PLACEMENTS = ${literal(requireKey('placements'))} as const;

export const WORKBENCH_SLOT_BY_HOST = ${literal(requireKey('slotByHost'))} as const;

export const DATA_REQUIREMENT_KINDS = ${literal(requireKey('dataRequirementKinds'))} as const;

export const CAPABILITY_DEFAULTS = ${literal(requireKey('capabilityDefaults'))} as const;

export const CONTEXT_SCHEMA_VERSIONS = ${literal(requireKey('contextSchemaVersions'))} as const;

export const DESCRIPTOR_SCHEMA_VERSIONS = ${literal(requireKey('descriptorSchemaVersions'))} as const;

export const RESERVED_PLUGIN_IDS = ${literal(requireKey('reservedPluginIds'))} as const;
`;

writeFileSync(outPath, banner + body, 'utf-8');
console.log(`generated ${outPath} from ${manifestPath}`);
