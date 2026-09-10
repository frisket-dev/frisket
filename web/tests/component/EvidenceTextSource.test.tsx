// @vitest-environment jsdom
//
// These tests exercise the text renderer directly; store seeding and drawer
// navigation are only plumbing to feed that renderer. TextArtifactSource is
// the renderer that dispatches the two artifact shapes, so we mount it with
// typed evidence fixtures.
//
// Dropped vs the e2e: the `toBeInViewport()` scroll-into-view checks — a real
// layout property jsdom cannot measure. The reading-surface + highlight + no-
// fallback assertions (the actual bug the spec caught) are preserved.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, within, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TextArtifactSource } from '../../src/components/EvidenceViewer';
import { artifactRef, blobRef, evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';



const DESCRIPTION_TEXT =
  'The meeting concluded that the budget increase of $50,000 was approved unanimously by the board.';
const CITED_QUOTE = 'the budget increase of $50,000 was approved unanimously';

beforeEach(() => {
  // jsdom has no layout engine — scrollIntoView is undefined on elements.
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('evidence text renderer', () => {
  it('renders a row+json (no-blob) citation as a highlighted quote surface, no fallback', () => {
    const artifact = evidenceArtifact({
      artifact_kind: 'row',
      media_type: 'application/vnd.frisket.row+json',
      metadata: { source_columns: ['Description'] },
      artifact_ref: artifactRef({ artifact_kind: 'row', blob: null }),
      spans: [evidenceSpan({ span_kind: 'text', quote: CITED_QUOTE, snippet: CITED_QUOTE })],
    });
    render(<TextArtifactSource artifact={artifact} />);

    expect(screen.queryByTestId('evidence-artifact-fallback')).toBeNull();
    expect(screen.getByTestId('evidence-text-source')).toBeVisible();
    const highlight = screen.getAllByTestId('evidence-text-highlight')[0];
    expect(highlight).toHaveTextContent(CITED_QUOTE);
    // the field label surfaces the candidate source column
    expect(screen.getByTestId('evidence-text-field-label')).toHaveTextContent('Field: Description');
  });

  it('renders a text/plain (blob-backed) citation as the full source with the quote highlighted in place', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response(DESCRIPTION_TEXT, { status: 200 }))),
    );
    const artifact = evidenceArtifact({
      artifact_kind: 'file',
      media_type: 'text/plain',
      filename: 'notes.txt',
      artifact_ref: artifactRef({
        artifact_kind: 'file',
        media_type: 'text/plain',
        blob: blobRef('/blob/notes.txt'),
      }),
      spans: [evidenceSpan({ span_kind: 'text', quote: CITED_QUOTE, snippet: CITED_QUOTE })],
    });
    render(<TextArtifactSource artifact={artifact} />);

    // Full source text (not just the quote) once the blob resolves.
    const body = await screen.findByTestId('evidence-text-body');
    expect(body).toHaveTextContent(DESCRIPTION_TEXT);
    expect(screen.queryByTestId('evidence-artifact-fallback')).toBeNull();
    const highlight = within(body).getAllByTestId('evidence-text-highlight')[0];
    expect(highlight).toHaveTextContent(CITED_QUOTE);
  });
});
