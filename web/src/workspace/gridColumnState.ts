// Grid column-state helpers: filter condition construction and the
// per-project+sheet localStorage persistence for column order / frozen count /
// hidden columns. Consumed by the workspace view-model hook and grid menus.
import { entityTypeName } from '../components/action-panel/nerLabelModel';
import { failureOutcomeLabel } from '../runFailureTaxonomy';
import type {
  GridFilterEntityValue,
  GridFilterListSelector,
  GridFilterOperator,
  GridFilterRangeValue,
  GridFilterRelativeDateValue,
  GridFilterSpec,
  GridFilterValue,
  GridSortDirection,
  GridSortSpec,
  SheetMeta,
} from '../api/open';

const DATE_PRESET_OPERATORS = new Set<GridFilterOperator>([
  'date_this_year',
  'date_ytd',
  'date_invalid',
]);
// Typed 400 codes that mean "the index is not searchable yet — refresh it",
// mirrored from embeddings/constants.ts's REFRESH_NEEDED_CODES. A lens-open
// hitting one of these is BLOCKED (no partial grid), not crashed.
export const LENS_REFRESH_NEEDED_CODES = new Set([
  'embedding_index_incomplete',
  'embedding_source_stale',
  'embedding_anchor_stale',
  'embedding_index_scope_invalid',
  'scope_invalid',
]);

const isGridFilterRangeValue = (value: GridFilterValue | undefined): value is GridFilterRangeValue =>
  typeof value === 'object' && value !== null && 'start' in value && 'end' in value;

const isGridFilterRelativeDateValue = (
  value: GridFilterValue | undefined,
): value is GridFilterRelativeDateValue =>
  typeof value === 'object' && value !== null && 'amount' in value && 'unit' in value;

// ---------------------------------------------------------------------------
// entity_eq and date_relative are the STRUCTURED grid filter values.
//
// Everything else this module carries is a scalar or a two-scalar range, and
// the code below used to assume that: the saved-view normalizer's catch-all
// did `String(raw)` and the chip label did `${column} ${operator} ${value}`.
// Handing either an entity payload produced the literal "[object Object]" —
// once in a saved view's persisted spec, and once on screen. Hence an explicit
// branch in BOTH, plus the parse/validate pair below. Relative dates likewise
// need an explicit round-trip branch so saved views stay dynamic.
// ---------------------------------------------------------------------------

/** The sentinel a malformed `entity_eq` normalizes to. It is deliberately NOT
 *  a drop: dropping the condition would silently WIDEN a saved view's row set
 *  (the user sees more rows than they saved, with no indication why). An empty
 *  `type` fails `entity_eq`'s "type must be a non-empty string" rule, so the
 *  chip renders as an explicit invalid-filter label and the server rejects the
 *  query loudly instead of quietly answering a different question. */
export const INVALID_ENTITY_FILTER_VALUE: GridFilterEntityValue = { type: '' };
/** An intentionally server-invalid sentinel for malformed saved multi-selects.
 * Keeping the column condition prevents restoration from silently widening the
 * saved view to every row. */
export const INVALID_IN_FILTER_VALUE: string[] = [];
/** An intentionally server-invalid sentinel for a malformed collection filter.
 * As with scalar multi-selects, preserving it prevents a saved view from
 * silently widening to every row. */
export const INVALID_LIST_CONTAINS_ANY_FILTER_VALUE: GridFilterListSelector[] = [];

const ENTITY_FILTER_KEYS = new Set(['type', 'text', 'fingerprint']);

function listSelectorKey(selector: GridFilterListSelector): string {
  return selector.kind === 'scalar'
    ? `scalar:${typeof selector.value}:${JSON.stringify(selector.value)}`
    : `entity:${JSON.stringify(selector.type)}:${JSON.stringify(selector.text)}`;
}

/** Parse the closed, server-authored list-member selector contract. Generic
 * JSON objects are intentionally not accepted: only scalar list members and
 * the known `{type, text}` entity shape have comparison semantics. */
export function listContainsAnyFilterValue(raw: unknown): GridFilterListSelector[] | null {
  if (!Array.isArray(raw) || raw.length === 0 || raw.length > 100) return null;
  const selectors: GridFilterListSelector[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) return null;
    const candidate = item as Record<string, unknown>;
    let selector: GridFilterListSelector | null = null;
    if (
      Object.keys(candidate).length === 2
      && candidate.kind === 'scalar'
      && (typeof candidate.value === 'string'
        || typeof candidate.value === 'boolean'
        || (typeof candidate.value === 'number' && Number.isFinite(candidate.value)))
    ) {
      selector = { kind: 'scalar', value: candidate.value };
    } else if (
      Object.keys(candidate).length === 3
      && candidate.kind === 'entity'
      && typeof candidate.type === 'string'
      && candidate.type !== ''
      && typeof candidate.text === 'string'
      && candidate.text !== ''
    ) {
      selector = { kind: 'entity', type: candidate.type, text: candidate.text };
    }
    if (!selector) return null;
    const key = listSelectorKey(selector);
    if (seen.has(key)) continue;
    seen.add(key);
    selectors.push(selector);
  }
  return selectors.length > 0 ? selectors : null;
}

/** Parse an untrusted `entity_eq` payload against the closed contract: a
 *  non-empty `type`, at most one of `text`/`fingerprint` (non-empty when
 *  present), and no other keys. Returns null when it does not conform. */
export function entityFilterValue(raw: unknown): GridFilterEntityValue | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const candidate = raw as Record<string, unknown>;
  for (const key of Object.keys(candidate)) {
    if (!ENTITY_FILTER_KEYS.has(key)) return null;
  }
  const { type, text, fingerprint } = candidate;
  if (typeof type !== 'string' || type === '') return null;
  if (text !== undefined && fingerprint !== undefined) return null;
  if (text !== undefined) {
    return typeof text === 'string' && text !== '' ? { type, text } : null;
  }
  if (fingerprint !== undefined) {
    return typeof fingerprint === 'string' && fingerprint !== ''
      ? { type, fingerprint }
      : null;
  }
  return { type };
}

export function filterConditionFromDraft(
  operator: GridFilterOperator,
  rawValue: string,
  rawStart: string,
  rawEnd: string,
): Partial<Record<GridFilterOperator, GridFilterValue>> {
  // The scalar editor cannot author a structured payload. Reaching here with
  // entity_eq would emit `{entity_eq: "<typed text>"}` — a filter that looks
  // applied, passes no validation, and silently answers a different question.
  // Unreachable in production (filterOperatorOptions never offers entity_eq),
  // so a throw is a programming-error alarm, not a user-facing path.
  if (operator === 'entity_eq' || operator === 'list_contains_any') {
    throw new Error(
      `${operator} is a programmatic filter and cannot be built from the scalar filter draft`,
    );
  }
  if (operator === 'between') {
    return { between: { start: rawStart.trim(), end: rawEnd.trim() } };
  }
  if (operator === 'date_relative') {
    const amount = Number(rawValue);
    if (!Number.isInteger(amount) || amount < 1 || amount > 10000) {
      throw new Error('relative date amount must be an integer from 1 to 10000');
    }
    const unit = rawStart as GridFilterRelativeDateValue['unit'];
    if (unit !== 'days' && unit !== 'weeks' && unit !== 'months') {
      throw new Error('relative date unit must be days, weeks, or months');
    }
    return { date_relative: { amount, unit } };
  }
  if (DATE_PRESET_OPERATORS.has(operator)) {
    return { [operator]: 'true' };
  }
  if (operator === 'gte' || operator === 'lte') {
    return { [operator]: (rawStart || rawValue).trim() };
  }
  return { [operator]: rawValue.trim() };
}

export const columnOrderKey = (projectId: string, sheetId: string) =>
  `frisket:column-order:${projectId}:${sheetId}`;

export const frozenColumnsKey = (projectId: string, sheetId: string) =>
  `frisket:frozen-columns:${projectId}:${sheetId}`;

// User-hidden columns are a per-project+sheet UI preference. Hidden ≠
// deleted: the data/schema are untouched; the Detail panel and catalog forms
// still read sheet.columns.
export const hiddenColumnsKey = (projectId: string, sheetId: string) =>
  `frisket:hidden-columns:${projectId}:${sheetId}`;

export const shownDefaultColumnsKey = (projectId: string, sheetId: string) =>
  `frisket:shown-default-columns:${projectId}:${sheetId}`;

export function loadHiddenColumns(projectId: string, sheet: SheetMeta): string[] {
  try {
    const raw = localStorage.getItem(hiddenColumnsKey(projectId, sheet.id));
    const parsed = raw ? (JSON.parse(raw) as string[]) : [];
    const known = new Set(sheet.columns.map((col) => col.name));
    const shownRaw = localStorage.getItem(shownDefaultColumnsKey(projectId, sheet.id));
    const shown = new Set(shownRaw ? (JSON.parse(shownRaw) as string[]) : []);
    const defaults: string[] = [];
    for (const column of sheet.columns) {
      if (column.defaultHidden && !shown.has(column.name)) defaults.push(column.name);
    }
    // Drop stale names (renamed/deleted columns) so a hidden set can never
    // reference a column that no longer exists.
    const hidden: string[] = [];
    for (const name of new Set([...parsed, ...defaults])) {
      if (known.has(name)) hidden.push(name);
    }
    return hidden;
  } catch {
    const defaults: string[] = [];
    for (const column of sheet.columns) {
      if (column.defaultHidden) defaults.push(column.name);
    }
    return defaults;
  }
}

export function clampFrozenColumnCount(count: number, visibleColumnCount: number): number {
  if (!Number.isFinite(count)) return Math.min(1, visibleColumnCount);
  return Math.max(0, Math.min(Math.round(count), visibleColumnCount));
}

export function normalizeColumnOrder(order: string[] | null | undefined, sheet: SheetMeta): string[] {
  const known = new Set(sheet.columns.map((col) => col.name));
  const out = [];
  const seen = new Set<string>();
  for (const name of order ?? []) {
    if (!known.has(name) || seen.has(name)) continue;
    out.push(name);
    seen.add(name);
  }
  for (const column of sheet.columns) {
    if (seen.has(column.name)) continue;
    out.push(column.name);
    seen.add(column.name);
  }
  return out;
}

export function exactViewHiddenColumns(
  sheet: SheetMeta,
  visibleColumns: readonly string[],
  separatelyManagedHiddenColumns: ReadonlySet<string> = new Set<string>(),
): string[] {
  const visible = new Set(visibleColumns);
  return sheet.columns
    .map((column) => column.name)
    .filter((name) => !visible.has(name) && !separatelyManagedHiddenColumns.has(name));
}

/** Place a newly created column's name beside its anchor in a display
 *  columnOrder. The order pins names, so a name it omits renders at the far
 *  right regardless of schema position; insert-left/right must splice. A
 *  missing anchor falls back to appending (the no-order behavior). */
export function spliceColumnOrderBeside(
  order: readonly string[],
  newName: string,
  anchorName: string,
  side: 'left' | 'right',
): string[] {
  const next = order.filter((name) => name !== newName);
  const at = next.indexOf(anchorName);
  next.splice(at === -1 ? next.length : at + (side === 'right' ? 1 : 0), 0, newName);
  return next;
}

export function loadColumnOrder(projectId: string, sheet: SheetMeta): string[] {
  try {
    const raw = localStorage.getItem(columnOrderKey(projectId, sheet.id));
    return normalizeColumnOrder(raw ? JSON.parse(raw) as string[] : null, sheet);
  } catch {
    return normalizeColumnOrder(null, sheet);
  }
}

export function loadFrozenColumnCount(
  projectId: string,
  sheetId: string,
  visibleColumnCount: number,
): number {
  try {
    const raw = localStorage.getItem(frozenColumnsKey(projectId, sheetId));
    return clampFrozenColumnCount(raw === null ? 1 : Number(raw), visibleColumnCount);
  } catch {
    return Math.min(1, visibleColumnCount);
  }
}

export function hasCustomColumnOrder(order: string[] | null | undefined, sheet: SheetMeta): boolean {
  const normalized = normalizeColumnOrder(order, sheet);
  return normalized.some((name, index) => sheet.columns[index]?.name !== name);
}

export function normalizeGridFilterSpec(value: unknown): GridFilterSpec | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const out: GridFilterSpec = {};
  for (const [column, operators] of Object.entries(value)) {
    if (!operators || typeof operators !== 'object' || Array.isArray(operators)) continue;
    for (const operator of [
      'eq',
      'in',
      'neq',
      'contains',
      'gte',
      'lte',
      'between',
      'date_relative',
      'date_this_year',
      'date_ytd',
      'date_year',
      'date_month',
      'date_weekday',
      'date_invalid',
      'bbox',
      'failed',
      'entity_eq',
      'list_contains_any',
    ] as const) {
      const raw = (operators as Partial<Record<GridFilterOperator, unknown>>)[operator];
      if (raw === undefined || raw === null) continue;
      if (
        operator === 'bbox' &&
        typeof raw === 'object' &&
        !Array.isArray(raw) &&
        raw !== null
      ) {
        const b = raw as Record<string, unknown>;
        const nums = ['min_lon', 'min_lat', 'max_lon', 'max_lat'].map((k) => Number(b[k]));
        if (nums.every((n) => Number.isFinite(n))) {
          out[column] = {
            bbox: {
              min_lon: nums[0],
              min_lat: nums[1],
              max_lon: nums[2],
              max_lat: nums[3],
            },
          };
        }
      } else if (
        operator === 'between' &&
        typeof raw === 'object' &&
        !Array.isArray(raw) &&
        raw !== null
      ) {
        const range = raw as { start?: unknown; end?: unknown };
        if (range.start !== undefined && range.end !== undefined) {
          out[column] = {
            between: {
              start: String(range.start ?? ''),
              end: String(range.end ?? ''),
            },
          };
        }
      } else if (
        operator === 'date_relative' &&
        typeof raw === 'object' &&
        !Array.isArray(raw) &&
        raw !== null
      ) {
        const relative = raw as { amount?: unknown; unit?: unknown };
        const amount = Number(relative.amount);
        const unit = relative.unit;
        const keys = Object.keys(relative);
        const valid = keys.length === 2 && keys.includes('amount') && keys.includes('unit')
          && Number.isInteger(amount)
          && amount >= 1
          && amount <= 10000
          && (unit === 'days' || unit === 'weeks' || unit === 'months');
        out[column] = {
          date_relative: {
            amount: valid ? amount : 0,
            unit: valid ? unit : 'days',
          },
        };
      } else if (operator === 'date_relative') {
        // Keep malformed saved filters loudly invalid instead of dropping them
        // and silently widening the restored row set.
        out[column] = { date_relative: { amount: 0, unit: 'days' } };
      } else if (operator === 'in') {
        const values = Array.isArray(raw)
          ? raw.filter((item): item is string => typeof item === 'string')
          : [];
        const valid = Array.isArray(raw)
          && values.length > 0
          && values.length === raw.length
          && values.length <= 100;
        out[column] = { in: valid ? [...new Set(values)] : INVALID_IN_FILTER_VALUE };
      } else if (operator === 'entity_eq') {
        // The structured branch. Without it the catch-all below would write
        // the string "[object Object]" back into the saved view and into the
        // request, turning a mention filter into a text match that can never
        // hit. A payload that fails the closed contract is kept as the invalid
        // sentinel rather than dropped — see INVALID_ENTITY_FILTER_VALUE.
        out[column] = { entity_eq: entityFilterValue(raw) ?? INVALID_ENTITY_FILTER_VALUE };
      } else if (operator === 'list_contains_any') {
        out[column] = {
          list_contains_any: listContainsAnyFilterValue(raw) ?? INVALID_LIST_CONTAINS_ANY_FILTER_VALUE,
        };
      } else if (operator !== 'between' && operator !== 'bbox') {
        out[column] = { [operator]: String(raw) };
      }
    }
  }
  return Object.keys(out).length > 0 ? out : null;
}

export function normalizeGridSortSpec(value: unknown): GridSortSpec | null {
  if (!Array.isArray(value)) return null;
  const out: GridSortSpec = value.flatMap((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
    const raw = item as { column?: unknown; dir?: unknown };
    if (typeof raw.column !== 'string') return [];
    if (raw.dir !== 'asc' && raw.dir !== 'desc') return [];
    return [{ column: raw.column, dir: raw.dir as GridSortDirection }];
  });
  return out.length > 0 ? out : null;
}

/** Chip text for an `entity_eq` filter. It must say WHICH of the three
 *  filters is applied, because the row sets differ sharply: "grouped forms"
 *  is every spelling sharing one fingerprint, "exact spelling" is one literal
 *  surface, and the bare type is every mention of that type. The words
 *  "entity", "match", "resolved identity" and "cluster" are avoided
 *  deliberately — the fingerprint groups SPELLINGS, it does not resolve
 *  identities. The fingerprint itself is never printed:
 *  it is an internal comparison token, not a spelling anyone wrote. */
function entityFilterLabel(
  column: string,
  value: GridFilterValue | undefined,
  valueLabel: string | null | undefined,
): string {
  const entity = entityFilterValue(value);
  // Loud, not silent: a filter the client cannot describe is one the server
  // will reject, and the chip says so instead of showing "[object Object]".
  if (!entity) return `${column} invalid mentions filter`;
  // The SINGULAR display name, not the raw canonical type: `norp` and `fac` are
  // unguessable and `work_of_art` is not a word. Not `entityTypeLabel` either —
  // those are the PLURALS the NER form's checkboxes use, and a chip describes
  // one group ("entities People mentions" reads worse than the raw type).
  const typeName = entityTypeName(entity.type);
  if (entity.text !== undefined) {
    return `${column} · “${entity.text}” (${typeName}, exact spelling)`;
  }
  if (entity.fingerprint !== undefined) {
    // The fingerprint carries no spelling, and printing the token itself would
    // show a comparison key nobody wrote. `valueLabel` is the spelling the
    // clicked group is known by, carried alongside the applied filter from the
    // one site that has it (the Mentions panel's group click, via
    // gridViewStore's applied.filterValueLabel). Without it — a saved view
    // restored, or a filter applied from anywhere else — the chip names the
    // TYPE rather than inventing a spelling.
    return valueLabel
      ? `${column} · “${valueLabel}” (${typeName}, grouped forms)`
      : `${column} · ${typeName} mentions (grouped forms)`;
  }
  return `${column} · all ${typeName} mentions`;
}

/** `valueLabel` = gridViewStore's `applied.filterValueLabel`, the spelling the
 *  applied filter's value is known by when its payload cannot carry one. Only
 *  the `entity_eq` fingerprint branch can use it; every other filter kind
 *  already prints its own value and ignores it. */
function gridFilterConditionLabel(
  column: string,
  operators: Partial<Record<GridFilterOperator, GridFilterValue>>,
  valueLabel?: string | null,
): string {
  const [operator, value] = Object.entries(operators ?? {})[0] ?? ['eq', ''];
  if (operator === 'between' && isGridFilterRangeValue(value)) {
    return `${column} between ${value.start} and ${value.end}`;
  }
  if (operator === 'date_relative' && isGridFilterRelativeDateValue(value)) {
    return `${column} in the last ${value.amount} ${value.unit}`;
  }
  if (operator === 'date_this_year') return `${column} this year`;
  if (operator === 'date_ytd') return `${column} year to date`;
  if (operator === 'date_year') return `${column} in ${value}`;
  if (operator === 'date_month') {
    const month = Number(value);
    const label = Number.isInteger(month) && month >= 1 && month <= 12
      ? new Intl.DateTimeFormat(undefined, { month: 'long', timeZone: 'UTC' })
          .format(new Date(Date.UTC(2020, month - 1, 1)))
      : String(value);
    return `${column} in ${label}`;
  }
  if (operator === 'date_weekday') {
    const weekdays = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    return `${column} on ${weekdays[Number(value)] ?? String(value)}`;
  }
  if (operator === 'date_invalid') return `${column} is not a valid date`;
  if (operator === 'bbox') {
    return `${column} in map area`;
  }
  if (operator === 'failed') {
    return value === 'any'
      ? `${column} failed cells`
      : `${column} failed: ${failureOutcomeLabel(String(value))}`;
  }
  if (operator === 'entity_eq') {
    return entityFilterLabel(column, value, valueLabel);
  }
  if (operator === 'list_contains_any') {
    const selectors = listContainsAnyFilterValue(value);
    if (!selectors) return `${column} invalid list filter`;
    const labels = selectors.map((selector) => selector.kind === 'scalar'
      ? String(selector.value)
      : `${selector.text} (${entityTypeName(selector.type)})`);
    if (labels.length <= 3) return `${column} contains ${labels.join(' or ')}`;
    return `${column} contains ${labels.slice(0, 2).join(', ')} or ${labels.length - 2} more`;
  }
  if (operator === 'in' && Array.isArray(value)) {
    if (value.length === 0) return `${column} invalid multi-value filter`;
    if (value.length <= 3) return `${column} is ${value.join(' or ')}`;
    return `${column} is ${value.slice(0, 2).join(', ')} or ${value.length - 2} more`;
  }
  return `${column} ${operator} ${value}`;
}

export function gridFilterLabel(filter: GridFilterSpec, valueLabel?: string | null): string {
  const labels = Object.entries(filter).map(([column, operators]) =>
    gridFilterConditionLabel(column, operators, valueLabel));
  return labels.length > 0 ? labels.join(' · ') : 'active';
}

export function gridSortLabel(sort: GridSortSpec): string {
  const first = sort[0];
  return first ? `${first.column} ${first.dir}` : 'active';
}
