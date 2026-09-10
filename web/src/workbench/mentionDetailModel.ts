// The mention-detail panel's pure model: what identifies the mention being
// shown, and the `entity_eq` payload it corresponds to. Split out of the .tsx
// so react-refresh/only-export-components stays satisfied and so the identity
// rules are testable without a DOM (the mentionsPanelModel.ts precedent).
import type { EntityMentionOccurrence, GridFilterEntityValue } from '../api/types';
import type { AnnotationMark } from './textAnnotationModel';

/** What identifies the mention being shown. Exactly one of the two identity
 *  modes — the same pair the Mentions panel's selectors carry (D6). */
export interface MentionDetailTarget {
  sheetId: string;
  /** The entity-mentions column the counts aggregate over. */
  columnId: string;
  type: string;
  /** Null for an unfingerprinted type: the group IS the exact spelling. */
  fingerprint: string | null;
  /** The spelling that was clicked — the panel's headline either way. */
  label: string;
}

/** Stable identity for the fetch, so re-clicking the same mention does not
 *  refetch and clicking a different one always does. */
export function mentionDetailKey(target: MentionDetailTarget): string {
  return `${target.sheetId}:${target.columnId}:${target.type}:${target.fingerprint ?? `t:${target.label}`}`;
}

/** The `entity_eq` payload this mention corresponds to, so "filter the grid to
 *  these rows" stays ONE construction shared with the Mentions panel. */
export function mentionDetailFilterValue(target: MentionDetailTarget): GridFilterEntityValue {
  return target.fingerprint !== null
    ? { type: target.type, fingerprint: target.fingerprint }
    : { type: target.type, text: target.label };
}

/** The mention a clicked mark belongs to.
 *
 *  A mark with no fingerprint is addressed by its exact spelling rather than
 *  refused: that is the correct identity for the unfingerprinted types, and for
 *  anything else it degrades to the narrower, still-true group instead of to
 *  nothing. A mention with no fingerprint still lists by exact text. */
export function mentionTargetForMark(
  mark: AnnotationMark,
  sheetId: string,
): MentionDetailTarget | null {
  if (mark.entityType === null) return null;
  return {
    sheetId,
    columnId: mark.entityColumnId,
    type: mark.entityType,
    fingerprint: mark.entityFingerprint,
    label: mark.quote,
  };
}

/** A snippet split into the three runs a result line draws: the context before
 *  the mention, the mention itself, and the context after. */
export interface SnippetParts {
  before: string;
  mark: string;
  after: string;
  /** Text was dropped at this edge — the server says so rather than the client
   *  assuming it, so the ellipsis is never drawn on a snippet that IS the whole
   *  cell. */
  truncatedStart: boolean;
  truncatedEnd: boolean;
}

/** Cut one snippet at the server's UTF-16 offsets.
 *
 *  `slice` on a JavaScript string indexes UTF-16 code units, which is exactly
 *  the unit the route returns, so this is a direct cut and never a search for
 *  the quote in the text (EvidenceViewer's model, which marks the wrong "Ada"
 *  the moment a name appears twice — and a snippet is precisely a window where
 *  it appears more than once).
 *
 *  Offsets outside the snippet degrade to "no mark" rather than to a clamped
 *  range: a clamped highlight is a coordinate claim nobody made. */
export function snippetParts(occurrence: EntityMentionOccurrence): SnippetParts {
  const { text, markStart, markEnd, truncatedStart, truncatedEnd } = occurrence.snippet;
  const usable =
    Number.isInteger(markStart)
    && Number.isInteger(markEnd)
    && markStart >= 0
    && markEnd <= text.length
    && markStart < markEnd;
  return {
    before: usable ? text.slice(0, markStart) : text,
    mark: usable ? text.slice(markStart, markEnd) : '',
    after: usable ? text.slice(markEnd) : '',
    truncatedStart,
    truncatedEnd,
  };
}
