// Presentation-only helpers shared by ActionPanel.tsx (ActionForm textareas,
// generic column selects, param-driven column pickers, NER label chips) and the
// extracted action-panel leaf controls (SourceInputControl.tsx, ActionParams.tsx,
// OutputFieldsEditor.tsx). They live in their own .ts module — not inside a
// component .tsx — per the repo fast-refresh idiom (see
// src/grid/columnAnnotations.tsx: react-refresh/only-export-components forbids
// non-component exports from component files).
import type { FormEvent } from 'react';
import type { ActionParam, ColumnDef } from '../../api/open';
import type { PanelSelectOption } from '../PanelSelect';

/** How a form sources row input: a single column or a {{column}} template.
 *  Single-sourced here (a pure .ts) so both the SourceInputControl leaf and
 *  the actionFormState buffer can share it — the state module may not import
 *  .tsx views (decomposition ledger). */
export type SourceMode = 'column' | 'template';

/** Sentinel for "create a new column" in save-to targets. Single-sourced for
 *  the same reason: TargetSaveToControl renders it, actionFormState defaults
 *  to it, and ActionForm's collision logic compares against it. */
export const NEW_COLUMN = '__new__';

/** Case-insensitive/trimmed existing-column lookup by name. Single-sourced
 *  here (not TargetSaveToControl.tsx) for the same .tsx-import restriction:
 *  TargetSaveToControl's OutputNameCombobox (overwrite warning, the "New
 *  column" badge) AND actionFormState's default-name collision seeding both
 *  need the exact same matching rule. */
export function existingColumnByName(columns: ColumnDef[], name: string): ColumnDef | undefined {
  const normalized = name.trim().toLowerCase();
  if (!normalized) return undefined;
  return columns.find((column) => column.name.trim().toLowerCase() === normalized);
}

/** Seed an unused output name; each logical output is named independently. */
export function dedupeDefaultColumnName(
  base: string,
  columns: ColumnDef[],
): string {
  const collides = (candidate: string) => Boolean(existingColumnByName(columns, candidate));
  if (!collides(base)) return base;
  const maxAttempts = columns.length + 1;
  let candidate = 2;
  let name = `${base}_${candidate}`;
  while (collides(name) && candidate <= maxAttempts) {
    candidate += 1;
    name = `${base}_${candidate}`;
  }
  return name;
}

/** Client-side mirror of recognized single-media URLs for automatic routing
 *  suggestions. It does not limit the explicit Download media action, which
 *  accepts any HTTP(S) URL. */
export function looksLikeSupportedMediaUrl(value: string): boolean {
  try {
    const parsed = new URL(value.trim());
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
    const rawHost = parsed.hostname.toLowerCase();
    const host = rawHost.replace(/^www\./, '');
    if (host === 'youtu.be') return /^\/[A-Za-z0-9_-]{4,}$/.test(parsed.pathname);
    if (host === 'youtube.com' || host.endsWith('.youtube.com')) {
      return (
        (parsed.pathname.replace(/\/$/, '') === '/watch' && Boolean(parsed.searchParams.get('v')))
        || /^\/(?:shorts|live|embed)\/[A-Za-z0-9_-]{4,}\/?$/.test(parsed.pathname)
      );
    }
    if (
      (host === 'tiktok.com' || host.endsWith('.tiktok.com'))
      && /^\/@[^/]+\/video\/\d+\/?$/.test(parsed.pathname)
    ) return true;
    if (rawHost === 'vm.tiktok.com' || rawHost === 'vt.tiktok.com') {
      return /^\/[A-Za-z0-9_-]+\/?$/.test(parsed.pathname);
    }
    if (rawHost === 'www.tiktok.com') {
      return /^\/t\/[A-Za-z0-9_-]+\/?$/.test(parsed.pathname);
    }
    if (host === 'vimeo.com' || host.endsWith('.vimeo.com')) {
      return /^\/(?:video\/)?\d+\/?$/.test(parsed.pathname);
    }
    if (host === 'kick.com' || host.endsWith('.kick.com')) {
      return (
        /^\/[\w-]+\/videos\/[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}\/?$/i.test(parsed.pathname)
        || /^\/[\w-]+\/clips\/clip_[\w-]+\/?$/i.test(parsed.pathname)
        || (/^\/[\w-]+\/?$/.test(parsed.pathname) && parsed.searchParams.has('clip'))
      );
    }
    return false;
  } catch {
    return false;
  }
}

/** Column list → PanelSelect options with the ⚡ marker on AI columns
 *  (`ai` is ColumnAiMeta on real columns; any truthy value counts). */
export function columnSelectOptions(
  columns: { name: string; type: string; ai?: unknown }[],
): PanelSelectOption[] {
  return columns.map((column) => ({
    value: column.name,
    label: `${column.name} (${column.type})`,
    ai: Boolean(column.ai),
  }));
}

/** Columns a param's column picker offers: every column unless the param
 *  declares eligible `columnTypes`. */
export function columnOptionsForParam(param: ActionParam, columns: ColumnDef[]): ColumnDef[] {
  return columns.filter((column) => (
    (!param.columnTypes?.length || param.columnTypes.includes(column.type))
    && (!param.aiGeneratedOnly || Boolean(column.ai))
  ));
}

/** The `columns` param-value encoding (MultiColumnPicker ↔ param string):
 *  newline/comma-separated names → trimmed, de-duplicated list. */
export function splitColumnParamValue(value: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of value.split(/[\n,]/)) {
    const trimmed = item.trim();
    if (trimmed && !seen.has(trimmed)) {
      seen.add(trimmed);
      out.push(trimmed);
    }
  }
  return out;
}

/** The comma-joined labels form-value encoding (NER label chips, category
 *  field labels): raw text → trimmed, non-empty label list. */
export function parseCommaLabels(value: string): string[] {
  return value.split(',').flatMap((label) => {
    const trimmed = label.trim();
    return trimmed ? [trimmed] : [];
  });
}

/** ActionForm.tsx re-derived a "raw string param → Number() → range-validate
 *  → boolean" gate three times (`semanticThresholdsOk`, `frameSamplingOk`,
 *  `maxDimensionOk`), each with a subtly different validation rule — only
 *  one of the three (`maxDimensionOk`) had an explicit empty-string guard
 *  and used `Number.isInteger` instead of `Number.isFinite`. This is the
 *  single shared gate: an explicit empty-string guard (never rely on
 *  `Number('') === 0` coincidentally clearing a bound) plus the
 *  integer-vs-finite choice and the min/max/lessThan/greaterThan bounds as
 *  parameters, so a new numeric gate can't independently drift on any of
 *  those axes again.
 *
 *  Defaults match `maxDimensionOk`'s prior behavior (the strictest of the
 *  three) — migrating the other two onto this makes them strictly SAFER for
 *  the (until now theoretical) case of a cleared/whitespace-only field, not
 *  more permissive: `frameSamplingOk`'s raw value is always non-empty by the
 *  time it reaches the gate (its caller falls back to a default string
 *  first), and `semanticThresholdsOk`'s empty case now correctly reads as
 *  invalid instead of silently satisfying its `>= 0` bound via `Number('')
 *  === 0`. */
export function numericParamOk(
  raw: string | null | undefined,
  {
    integer = false,
    min,
    max,
    lessThan,
    greaterThan,
  }: { integer?: boolean; min?: number; max?: number; lessThan?: number; greaterThan?: number } = {},
): boolean {
  const trimmed = (raw ?? '').trim();
  if (trimmed === '') return false;
  const value = Number(trimmed);
  if (!(integer ? Number.isInteger(value) : Number.isFinite(value))) return false;
  if (min !== undefined && value < min) return false;
  if (max !== undefined && value > max) return false;
  if (lessThan !== undefined && !(value < lessThan)) return false;
  if (greaterThan !== undefined && !(value > greaterThan)) return false;
  return true;
}

export function resizeTextareaToContent(textarea: HTMLTextAreaElement | null) {
  if (!textarea) return;
  textarea.style.height = 'auto';
  textarea.style.height = `${textarea.scrollHeight}px`;
}

export function handleAutoResizeTextareaInput(event: FormEvent<HTMLTextAreaElement>) {
  resizeTextareaToContent(event.currentTarget);
}
