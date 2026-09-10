// The middle pane's "Citation 1" chip and the evidence pane's "Citation" header
// block both showed ONLY the citation's rank-0 span quote — the backend chip
// summary (store/evidence.py's `_link_summary`) has always derived `snippet`
// from the single first-ranked span, so a multi-span citation read as
// truncated. Displays should err on the side of "here's the block of relevant
// information" — the user doesn't care that we're joining spans — so the user
// should see the WHOLE cited passage, not span 1 alone.
//
// This derives that full passage CLIENT-SIDE from the same full-fidelity
// payload the evidence viewer already fetches (`api.getEvidenceViewer`,
// EvidenceViewer.tsx's MediaOrFallback) rather than widening the backend
// summary — `_link_summary` is shared by every per-cell/per-column chip across
// the app (RowDrawer included) and owned by a different lane, so this is a pure
// frontend derivation over data the backend already returns in full.
//
// Merge rule: a citation's CITED spans (`required: true` — the same flag the
// temporal viewer's `citedSpanIds` already treat as "this span is part of what's
// cited") are joined IN ORDER. Spans that share a contiguous run (`run_index`,
// the backend's server-computed grouping) read as one continuous passage
// (joined with a space); a run boundary — a different run, a different
// artifact, or a span with no run_index at all (a text/page span, which the
// backend never groups into runs) — reads as a separate passage, joined with an
// ellipsis. A link with no spans flagged `required` at all (an older/simple
// citation that never got citation_required flagging) falls back to treating
// every one of its spans as cited, so the merge still surfaces the whole
// citation instead of silently degrading to the old single-span behavior.

import type { EvidenceSpan, EvidenceViewerPayload } from '../api/types';

function collectQuote(
  payload: EvidenceViewerPayload,
  predicate: (span: EvidenceSpan) => boolean,
): string | null {
  type Run = { key: string; parts: string[] };
  const runs: Run[] = [];
  for (const artifact of payload.artifacts) {
    for (const span of artifact.spans) {
      if (!predicate(span)) continue;
      const text = (span.snippet ?? span.quote ?? '').trim();
      if (!text) continue;
      const runKey =
        span.run_index !== null && span.run_index !== undefined
          ? `${artifact.stable_id}:run:${span.run_index}`
          : `${artifact.stable_id}:span:${span.stable_id}`;
      const last = runs[runs.length - 1];
      if (last && last.key === runKey) {
        last.parts.push(text);
      } else {
        runs.push({ key: runKey, parts: [text] });
      }
    }
  }
  if (runs.length === 0) return null;
  return runs.map((run) => run.parts.join(' ')).join(' … ');
}

/** The full cited passage for one evidence link's viewer payload — every
 * `required` span's quote, in citation order, contiguous runs merged into
 * one passage and disjoint runs separated by an ellipsis. `null` when the
 * link carries no quotable spans at all (a page/region citation with no
 * OCR'd text, or a payload that hasn't loaded yet). */
export function mergeCitedQuote(payload: EvidenceViewerPayload | null | undefined): string | null {
  if (!payload) return null;
  return collectQuote(payload, (span) => span.required) ?? collectQuote(payload, () => true);
}
