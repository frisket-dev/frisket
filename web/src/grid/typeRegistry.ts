// Column-type → presentation resolution.
//
// The grid does NOT switch on a hardcoded type enum: each column type carries
// presentation hints ({renderer, ...}) in the backend registry
// (GET /api/column-types). This module caches that registry — seeded with the
// core defaults so the grid renders before the fetch lands (and in mock
// mode) — and buildCell (cells.tsx) resolves a renderer function from it.
// A type whose renderer the frontend doesn't know falls back to text.

import type { ColumnTypeInfo } from '../api/open';

export interface TypePresentation {
  renderer?: string;
  [k: string]: unknown;
}

export type TypeFacetBehavior = {
  kind: 'categorical';
  preferred: boolean;
  oneClick: boolean;
  operator: 'eq' | 'contains';
} | {
  kind: 'range';
  valueKind: 'number' | 'integer' | 'date';
  preferred: false;
  oneClick: false;
} | {
  /** A JSON list whose server preview supplies selectable member selectors. */
  kind: 'collection';
  preferred: false;
  oneClick: false;
  operator: 'list_contains_any';
};

// Defaults mirror core registrations plus built-in semantic extensions in
// frisket/column_types.py. Project plugin types are added by the active
// project catalog and removed again when that catalog no longer contains them.
const DEFAULT_PRESENTATIONS: Array<[string, TypePresentation]> = [
  ['text', { renderer: 'text', facet: { kind: 'categorical', operator: 'eq' } }],
  ['date', { renderer: 'text', facet: { kind: 'range', valueKind: 'date' } }],
  ['number', { renderer: 'number', align: 'right', facet: { kind: 'range', valueKind: 'number' } }],
  ['integer', { renderer: 'integer', align: 'right', facet: { kind: 'range', valueKind: 'integer' } }],
  ['boolean', { renderer: 'boolean', facet: { kind: 'categorical', operator: 'eq' } }],
  ['category', { renderer: 'category', facet: { kind: 'categorical', preferred: true, oneClick: true, operator: 'eq' } }],
  ['json', { renderer: 'json', facet: { kind: 'collection', operator: 'list_contains_any' } }],
  ['image', { renderer: 'image' }],
  ['audio', { renderer: 'media', mediaType: 'audio' }],
  ['video', { renderer: 'media', mediaType: 'video' }],
  ['file', { renderer: 'media', mediaType: 'file' }],
  ['link', { renderer: 'link', facet: { kind: 'categorical', operator: 'eq' } }],
  ['geo_point', { renderer: 'map-pin' }],
  ['geo_shape', { renderer: 'map-overlay' }],
  ['timestamped_transcript', { renderer: 'text', label: 'Timestamped transcript', userSelectable: false }],
  ['timeline_point', { renderer: 'timeline', geometry: 'point', multiple: false, label: 'Timestamp', userSelectable: false }],
  ['timeline_points', { renderer: 'timeline', geometry: 'point', multiple: true, label: 'Timestamps', userSelectable: false }],
  ['timeline_range', { renderer: 'timeline', geometry: 'range', multiple: false, label: 'Time range', userSelectable: false }],
  ['timeline_ranges', { renderer: 'timeline', geometry: 'range', multiple: true, label: 'Time ranges', userSelectable: false }],
];

const presentations = new Map<string, TypePresentation>(DEFAULT_PRESENTATIONS);

/** Replace the local cache with the active catalog so stale plugin renderers disappear. */
export function applyColumnTypeRegistry(infos: ColumnTypeInfo[]): void {
  presentations.clear();
  for (const [name, presentation] of DEFAULT_PRESENTATIONS) {
    presentations.set(name, { ...presentation });
  }
  for (const info of infos) {
    // The active registry is authoritative. Defaults only cover the time
    // before the catalog is fetched (and mock mode); merging them into a live
    // entry could give a server type a renderer or facet it did not declare.
    presentations.set(info.name, { ...info.presentation });
  }
}

/** Presentation hints for a column type; {} for unknown types. */
export function presentationFor(type: string): TypePresentation {
  return presentations.get(type) ?? {};
}

/** Structured facet behavior carried by the column-type registry. Unknown or
 * malformed plugin hints degrade to no facet behavior; callers never infer
 * click semantics from a renderer name alone. */
export function facetBehaviorFor(type: string): TypeFacetBehavior | null {
  const raw = presentationFor(type).facet;
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const hint = raw as Record<string, unknown>;
  if (hint.kind === 'categorical') {
    if (Object.keys(hint).some((key) => !['kind', 'operator', 'preferred', 'oneClick'].includes(key))) {
      return null;
    }
    if (hint.operator !== 'eq' && hint.operator !== 'contains') return null;
    if (hint.preferred !== undefined && typeof hint.preferred !== 'boolean') return null;
    if (hint.oneClick !== undefined && typeof hint.oneClick !== 'boolean') return null;
    if (hint.valueKind !== undefined) return null;
    return {
      kind: 'categorical',
      preferred: hint.preferred === true,
      oneClick: hint.oneClick === true,
      operator: hint.operator,
    };
  }
  if (hint.kind === 'range') {
    if (Object.keys(hint).some((key) => !['kind', 'valueKind'].includes(key))) return null;
    if (hint.valueKind !== 'number' && hint.valueKind !== 'integer' && hint.valueKind !== 'date') {
      return null;
    }
    if (hint.operator !== undefined || hint.preferred !== undefined || hint.oneClick !== undefined) {
      return null;
    }
    return {
      kind: 'range',
      valueKind: hint.valueKind,
      preferred: false,
      oneClick: false,
    };
  }
  if (hint.kind === 'collection') {
    if (Object.keys(hint).some((key) => !['kind', 'operator'].includes(key))) return null;
    if (hint.operator !== 'list_contains_any') return null;
    return {
      kind: 'collection',
      preferred: false,
      oneClick: false,
      operator: 'list_contains_any',
    };
  }
  return null;
}
