// Typed ActionTemplate/SheetMeta/catalog-snapshot fixtures for ActionForm
// component tests. Catalog completeness comes from the production launcher
// registry; this file owns only the richer schemas and hints exercised across
// several ActionForm suites. Single-suite subjects stay beside their tests.

import type {
  ActionCatalogEntry,
  ActionCatalogPayload,
  ActionTemplate,
  ColumnDef,
  SheetMeta,
} from "../../src/api/types";
import {
  actionTemplatesFromCatalog,
} from "../../src/actions/model";
import {
  completeMappedActionCatalog,
  syntheticActionCatalogEntry,
} from "./actionCatalogFixtures";
import { servedActionCatalog } from "./servedActionCatalog";

const ACTION_FORM_SUBJECT_ENTRIES: readonly ActionCatalogEntry[] = [
  syntheticActionCatalogEntry("derive.join"),
  syntheticActionCatalogEntry("join.semantic"),
  syntheticActionCatalogEntry("map.columns_from_json"),
  syntheticActionCatalogEntry("map.ner"),
  syntheticActionCatalogEntry("media.ocr"),
  syntheticActionCatalogEntry("media.ytdlp_download"),
  syntheticActionCatalogEntry("temporal.extract_range"),
  syntheticActionCatalogEntry("derive.temporal_segments"),
  syntheticActionCatalogEntry("web.capture_page"),
  syntheticActionCatalogEntry("map.clean_column", {
    input_schema: {
      type: "object",
      additionalProperties: false,
      required: ["source"],
      properties: {
        source: { type: "string" },
        case: {
          type: "string",
          enum: ["keep", "smart_title", "title", "upper", "lower"],
          default: "smart_title",
        },
        null_tokens: {
          type: "string",
          default: "n/a,na,null,none,unknown,unk,tbd,-,--,?",
        },
        blank_null_tokens: { type: "boolean", default: true },
        lowercase_emails: { type: "boolean", default: true },
        normalize_us_phone: { type: "boolean", default: true },
        expand_abbreviations: { type: "boolean", default: true },
        reorder_person_name: { type: "boolean", default: true },
        canonicalize_duplicates: { type: "boolean", default: true },
        strip_edge_punct: { type: "boolean", default: false },
        normalize_unicode_punct: { type: "boolean", default: false },
        remove_thousands_separators: { type: "boolean", default: false },
        remove_all_commas: { type: "boolean", default: false },
        make_numeric: { type: "boolean", default: false },
      },
    },
    required_capabilities: ["project:write"],
    writes_project: true,
    ui_hints: {
      form: "generated",
      category: "cleanup",
      semantic_controls: { source: "column" },
      source_requirements: [{
        id: "source",
        mode: "column",
        param: "source",
        label: "Column to clean",
        min: 1,
        max: 1,
        accepted_column_types: ["text", "category", "link"],
        accepted_cell_kinds: ["text"],
      }],
      logical_outputs: [{ key: "cleaned", column_type: "text" }],
    },
  }),
  syntheticActionCatalogEntry("map.clean_dates", {
    input_schema: {
      type: "object",
      additionalProperties: false,
      required: ["source"],
      properties: {
        source: { type: "string", title: "Source" },
        format: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
      },
    },
    required_capabilities: ["project:write"],
    writes_project: true,
    ui_hints: {
      form: "generated",
      category: "cleanup",
      semantic_controls: { source: "column" },
      source_requirements: [{
        id: "source",
        mode: "column",
        param: "source",
        label: "Date column",
        min: 1,
        max: 1,
        accepted_column_types: ["text", "date", "number", "integer"],
        accepted_cell_kinds: ["text", "blob", "template"],
      }],
      logical_outputs: [{ key: "cleaned", column_type: "date" }],
    },
  }),
  syntheticActionCatalogEntry("map.translate"),
  syntheticActionCatalogEntry("resolve.fill_missing", {
    input_schema: {
      type: "object",
      additionalProperties: false,
      properties: {
        source: { type: "string" },
        method: { type: "string", enum: ["down", "up", "value", "mean", "median", "mode"], default: "down" },
        fill_value: { type: "string" },
        treat_blank_as_missing: { type: "boolean", default: true },
      },
      required: ["source", "method"],
    },
    row_scope_policy: { kind: "sheet_rows", selectors: ["all_rows"] },
    ui_hints: {
      form: "generated",
      form_params: [
        { name: "method", type: "category", choices: ["down", "up", "value", "mean", "median", "mode"], default: "down", required: true },
        { name: "fill_value", type: "text", visible_when: { param: "method", value: "value" } },
        { name: "treat_blank_as_missing", type: "boolean", default: true },
      ],
      semantic_controls: { source: "column" },
      logical_outputs: [{ key: "cleaned", column_type: "text" }],
      category: "resolve",
      primary_fields: ["source", "method"],
      source_requirements: [{
        id: "source",
        mode: "column",
        param: "source",
        label: "Column to fill",
        min: 1,
        max: 1,
        accepted_column_types: ["text", "category", "link", "number", "integer", "date"],
      }],
    },
  }),
];

function actionFormCatalog(
  replacements: readonly ActionCatalogEntry[] = [],
  stockEntries: readonly ActionCatalogEntry[] = [],
): ActionCatalogPayload {
  const byKind = new Map(
    ACTION_FORM_SUBJECT_ENTRIES.map((entry) => [entry.kind, entry]),
  );
  for (const entry of replacements) byKind.set(entry.kind, entry);
  const mappedEntries: ActionCatalogEntry[] = [];
  const generatedEntries: ActionCatalogEntry[] = [];
  for (const entry of byKind.values()) {
    if (servedActionCatalog().actions.some((candidate) => candidate.kind === entry.kind)) {
      mappedEntries.push(entry);
    } else {
      generatedEntries.push(entry);
    }
  }
  return completeMappedActionCatalog({
    replacements: mappedEntries,
    stockEntries: [...generatedEntries, ...stockEntries],
  });
}

const COMPLETE_ACTION_CATALOG = actionFormCatalog();

const COMPLETE_RESOLVED_ACTION_TEMPLATES = actionTemplatesFromCatalog(COMPLETE_ACTION_CATALOG);

/** A template resolved from the shared catalog, with `overrides` merged
 *  shallowly on top. Throws for an unknown kind rather than silently
 *  returning an empty template. */
export function actionTemplateFor(
  kind: string,
  overrides: Partial<ActionTemplate> = {},
): ActionTemplate {
  const base = COMPLETE_RESOLVED_ACTION_TEMPLATES.find((template) => template.kind === kind);
  if (!base)
    throw new Error(`No ActionTemplate fixture base for kind "${kind}"`);
  return { ...base, ...overrides };
}

/** A template resolved through the complete served presentation fixture.
 * Unlike actionTemplateFor(), this includes the catalog's canonical action
 * kind and therefore exercises boundaries that require the real
 * launcher/canonical identity pair. */
export function catalogResolvedActionTemplateFor(
  canonicalKind: string,
  overrides: Partial<ActionTemplate> = {},
): ActionTemplate {
  const matches = COMPLETE_RESOLVED_ACTION_TEMPLATES.filter(
    (template) => template.actionKind === canonicalKind,
  );
  if (matches.length !== 1) {
    throw new Error(
      `Expected exactly one resolved ActionTemplate for canonical kind "${canonicalKind}"; found ${matches.length}`,
    );
  }
  return { ...matches[0], ...overrides };
}

export function sheetMeta(
  columns: ColumnDef[],
  overrides: Partial<SheetMeta> = {},
): SheetMeta {
  return {
    id: "sheet-1",
    name: "Sheet1",
    rowCount: 1,
    columns,
    citedColumnIds: [],
    annotatedTextColumnIds: [],
    ...overrides,
  };
}

/** A minimal, validly-typed (all-empty JSON-schema-like fields) catalog
 *  payload — its content is never read by ActionForm/the draft controller,
 *  only its non-null presence (selectDraftReadiness requires
 *  `catalog !== null` once `status === 'ready'`). */
export function catalogPayload(
  overrides: Partial<ActionCatalogPayload> = {},
): ActionCatalogPayload {
  return {
    schema_version: "frisket.action_catalog.v2",
    actions: [],
    action_schema: {},
    error_schema: {},
    result_schema: {},
    receipt_schema: {},
    validation_result_schema: {},
    ...overrides,
  };
}

/** A complete mapped presentation catalog with exact per-kind replacements.
 * ActionPanel consumes a total catalog and intentionally fails closed on
 * missing mapped entries, so focused panel tests replace their subject
 * without copying unrelated served entries. Explicit hidden stock subjects
 * are appended only when a focused test needs them. */
export function completeCatalogPayload(
  replacements: readonly ActionCatalogEntry[] = [],
  stockEntries: readonly ActionCatalogEntry[] = [],
): ActionCatalogPayload {
  return actionFormCatalog(replacements, stockEntries);
}
