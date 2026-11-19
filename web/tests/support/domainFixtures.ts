// Typed domain fixtures for component and unit tests.
//
// These build objects against the real exported wire/domain types
// (src/api/types.ts) rather than hand-rolled JSON, so a contract change to
// ColumnDef/Row/ColumnAiMeta breaks the fixtures at compile/run time instead
// of letting a stale mock drift. Every field is a real type field; callers
// override only what their assertion cares about.

import type { CellValue, ColumnAiMeta, ColumnDef, ColumnType, Row, RuntimeConfig } from '../../src/api/types';

export function aiMeta(overrides: Partial<ColumnAiMeta> = {}): ColumnAiMeta {
  return {
    actionName: 'Classify',
    prompt: 'do the thing',
    model: 'gemini/gemini-2.5-flash',
    costSoFar: 0,
    versions: [],
    ...overrides,
  };
}

export function columnDef(overrides: Partial<ColumnDef> = {}): ColumnDef {
  return {
    id: overrides.id ?? 'col-1',
    name: overrides.name ?? 'value',
    type: (overrides.type ?? 'text') as ColumnType,
    ...overrides,
  };
}

export function row(cells: Record<string, CellValue>, overrides: Partial<Row> = {}): Row {
  return {
    id: overrides.id ?? 'row-1',
    index: overrides.index ?? 0,
    cells,
    provenance: overrides.provenance ?? {},
    ...overrides,
  };
}

export function runtimeConfig(overrides: Partial<RuntimeConfig> = {}): RuntimeConfig {
  return {
    cache_mode: 'replay',
    live_calls_possible: true,
    cache_mode_editable: false,
    email_from_address: null,
    email_from_name: null,
    auth_methods: { password: true, magic_link: false, oidc: [] },
    plugins_available: false,
    plugin_management_available: false,
    product_telemetry_available: false,
    ...overrides,
  };
}
