// The annotated-text reader's pure model: turn a cell's annotation layers into
// something a renderer can walk in one pass, plus the per-family toggle rows and
// the dirty-layer summary. No React, no API calls — the AnnotatedTextReader.tsx
// / tests/unit split this directory already uses for its other view models.
//
// Rendering is FLAT TEXT + SERVER OFFSETS. Offsets arrive as
// UTF-16 code units, which is exactly what a JavaScript string indexes in, so
// `text.slice(start, end)` IS the mark. Nothing here searches the text for a
// quote — that is EvidenceViewer's model (`highlightQuotes`), which re-locates
// each quote by first occurrence and would put marks in the wrong place the
// moment a name appears twice.

import type {
  TextAnnotationLayer,
  TextAnnotationSpan,
  TextAnnotationUnpositionedReason,
} from '../api/types';

/** One occurrence, flattened out of its layer so the partition can carry it. */
export interface AnnotationMark {
  occurrenceId: string;
  toggleKey: string;
  layerFamily: string;
  entityType: string | null;
  /** Null for the unfingerprinted types, which the panel addresses by exact
   *  spelling instead (D6 / R-unfingerprinted). */
  entityFingerprint: string | null;
  /** The entity column the layer wrote into — what the mention-detail panel
   *  aggregates over. */
  entityColumnId: string;
  /** The occurrence's FULL range, kept on every fragment it was split into so a
   *  click on any piece opens the whole mention (D2). */
  start: number;
  end: number;
  quote: string;
}

/** A non-overlapping run of the text. `marks` is every occurrence covering it,
 *  outermost first. */
export interface AnnotationFragment {
  start: number;
  end: number;
  text: string;
  marks: AnnotationMark[];
  /** The INNERMOST covering mark — the one a click on this fragment belongs to.
   *  Null on plain (unmarked) text. */
  owner: AnnotationMark | null;
  /** True on the first fragment its `owner` occurrence appears in. Only that
   *  fragment enters tab order, so a mark split across three fragments is one
   *  tab stop, not three. */
  ownerFirstFragment: boolean;
}

/** A per-family on/off row in the reader's toggle strip. */
export interface AnnotationToggle {
  /** `${sheetId}:${outputColumnId}:${family}` — server-minted (D3). */
  toggleKey: string;
  layerFamily: string;
  label: string;
  /** The column the producer wrote its result into. The name is shown only to
   *  disambiguate two toggles of the same family (two NER runs, two columns);
   *  the id is where a replay of the stored action starts. */
  outputColumnId: string;
  outputColumnName: string | null;
  enabled: boolean;
  /** Marks currently drawable, or — when the layer cannot be positioned — its
   *  LAST-KNOWN total, which is the only honest number left. */
  count: number;
  positioned: boolean;
  unpositionedReason: TextAnnotationUnpositionedReason | null;
}

const FAMILY_LABELS: Record<string, string> = {
  entities: 'Entities',
};

/** The reader's name for a layer family. Unknown families are humanized rather
 *  than dropped: `layer_family` is an open slug, so an unregistered producer
 *  gets generic presentation instead of no presentation. */
export function layerFamilyLabel(family: string): string {
  const known = FAMILY_LABELS[family];
  if (known) return known;
  const words = family.trim().replace(/[_.-]+/g, ' ').replace(/\s+/g, ' ');
  if (!words) return family;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Copy for a layer whose marks cannot be drawn. Each reason names what
 *  actually happened; none of them says "error", because none of them is one —
 *  the result is intact, only its coordinates stopped applying. */
export function unpositionedExplanation(reason: TextAnnotationUnpositionedReason): string {
  switch (reason) {
    case 'content_hash_mismatch':
      return 'The source text changed after this result was created, so its highlights may no longer line up.';
    case 'unsupported_offset_unit':
      return 'This result stored its positions in a unit this reader cannot draw.';
    case 'invalid_geometry':
      return 'This result’s stored positions do not fit the current text.';
  }
}

/** The toggle strip, one row per `toggle_key` present on the cell. Layers that
 *  share a key (a defensive case — one run over one output cell is one link)
 *  are folded together, and a fold is positioned only if every member is. */
export function annotationToggles(
  layers: readonly TextAnnotationLayer[],
  disabledToggleKeys: readonly string[],
): AnnotationToggle[] {
  const disabled = new Set(disabledToggleKeys);
  const byKey = new Map<string, AnnotationToggle>();
  for (const layer of layers) {
    const existing = byKey.get(layer.toggleKey);
    const count = layer.positioned ? layer.counts.shown : layer.unpositioned.total;
    const reason = layer.positioned ? null : layer.unpositioned.reason;
    if (existing) {
      existing.count += count;
      existing.positioned = existing.positioned && layer.positioned;
      existing.unpositionedReason = existing.unpositionedReason ?? reason;
      continue;
    }
    byKey.set(layer.toggleKey, {
      toggleKey: layer.toggleKey,
      layerFamily: layer.layerFamily,
      label: layerFamilyLabel(layer.layerFamily),
      outputColumnId: layer.outputColumn.id,
      outputColumnName: layer.outputColumn.name,
      enabled: !disabled.has(layer.toggleKey),
      count,
      positioned: layer.positioned,
      unpositionedReason: reason,
    });
  }
  return [...byKey.values()];
}

/** Every drawable occurrence from the ENABLED, POSITIONED layers.
 *
 *  An unpositioned layer contributes nothing here on purpose: its count still
 *  shows in the toggle strip beside a `[!]`, but drawing its stored offsets
 *  over text that no longer matches them is exactly the confident-wrong
 *  highlight this whole coordinate substrate exists to prevent. */
export function collectMarks(
  layers: readonly TextAnnotationLayer[],
  disabledToggleKeys: readonly string[],
): AnnotationMark[] {
  const disabled = new Set(disabledToggleKeys);
  const marks: AnnotationMark[] = [];
  for (const layer of layers) {
    if (!layer.positioned) continue;
    if (disabled.has(layer.toggleKey)) continue;
    for (const span of layer.spans) {
      marks.push(spanToMark(span, layer));
    }
  }
  return marks;
}

function spanToMark(span: TextAnnotationSpan, layer: TextAnnotationLayer): AnnotationMark {
  return {
    occurrenceId: span.occurrenceId,
    toggleKey: layer.toggleKey,
    layerFamily: layer.layerFamily,
    entityType: span.entityType,
    entityFingerprint: span.entityFingerprint,
    entityColumnId: layer.outputColumn.id,
    start: span.start,
    end: span.end,
    quote: span.quote,
  };
}

/** What a screen reader is told about one mark (R18).
 *
 *  Marks nest — "Boeing" inside "Boeing Company" — and where they do, the
 *  visual answer (a thicker underline) has no spoken equivalent. So the label
 *  names the mark the click belongs to and then the marks around it, OUTERMOST
 *  LAST, which is the order the fragment's covering set already carries
 *  reversed. Deterministic by construction: the partition sorts start-ascending
 *  then longest-first, so the same overlap always announces the same way rather
 *  than depending on which producer wrote first.
 *
 *  Only the fragment that owns a tab stop gets a label; the continuation
 *  fragments of a split mark are not separately focusable and naming them again
 *  would read the same mention three times. */
export function markAriaLabel(fragment: AnnotationFragment): string | undefined {
  const owner = fragment.owner;
  if (owner === null || !fragment.ownerFirstFragment) return undefined;
  const name = (mark: AnnotationMark) =>
    `${mark.quote || fragment.text}, ${mark.entityType ?? 'entity'}`;
  const enclosing = fragment.marks
    .filter((mark) => mark.occurrenceId !== owner.occurrenceId)
    .reverse()
    .map((mark) => `within ${name(mark)}`);
  return [name(owner), ...enclosing].join(', ');
}

/** D2: partition the text at EVERY mark boundary into one non-overlapping
 *  fragment list.
 *
 *  Merging overlapping marks into one highlight (EvidenceViewer's model) loses
 *  the thing the reader is for: two spans of one family genuinely nest —
 *  "Boeing" inside "Boeing Company" — and a merged highlight can answer neither
 *  "which mention did I click" nor "how many are there". Partitioning keeps
 *  both: every fragment carries its covering occurrences with their ORIGINAL
 *  ranges, and the innermost one owns the click.
 *
 *  Marks outside the text, inverted, or empty are dropped rather than clamped:
 *  a clamped range is a coordinate claim nobody made. */
export function partitionAnnotationFragments(
  text: string,
  marks: readonly AnnotationMark[],
): AnnotationFragment[] {
  const length = text.length;
  if (length === 0) return [];
  const usable = marks
    .filter((mark) => mark.start >= 0 && mark.end <= length && mark.start < mark.end)
    // Start ascending, then LONGEST first, so the covering set of any fragment
    // is already outermost-first without a second sort per fragment.
    .toSorted((a, b) => a.start - b.start || b.end - a.end);
  if (usable.length === 0) {
    return [{ start: 0, end: length, text, marks: [], owner: null, ownerFirstFragment: false }];
  }

  const boundaries = new Set<number>([0, length]);
  for (const mark of usable) {
    boundaries.add(mark.start);
    boundaries.add(mark.end);
  }
  const cuts = [...boundaries].sort((a, b) => a - b);

  const fragments: AnnotationFragment[] = [];
  const seenOwners = new Set<string>();
  let next = 0;
  let active: AnnotationMark[] = [];
  for (let i = 0; i < cuts.length - 1; i += 1) {
    const start = cuts[i]!;
    const end = cuts[i + 1]!;
    active = active.filter((mark) => mark.end > start);
    while (next < usable.length && usable[next]!.start <= start) {
      const candidate = usable[next]!;
      if (candidate.end > start) active.push(candidate);
      next += 1;
    }
    // Innermost = the latest-starting cover; among equal starts, the shortest.
    // `active` is already start-asc/length-desc, so that is simply the last.
    const owner = active.length > 0 ? active[active.length - 1]! : null;
    const ownerFirstFragment = owner !== null && !seenOwners.has(owner.occurrenceId);
    if (owner !== null) seenOwners.add(owner.occurrenceId);
    fragments.push({
      start,
      end,
      text: text.slice(start, end),
      marks: active.slice(),
      owner,
      ownerFirstFragment,
    });
  }
  return fragments;
}
