import { describe, expect, it } from 'vitest';
import type { CitationViewerPayload } from '../../src/api/types';
import { mergeCitedPassage } from '../../src/workbench/citationPassage';
import { evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

function payload(truncated: boolean): CitationViewerPayload {
  return { artifacts: [evidenceArtifact({ spans: [evidenceSpan({
    quote: 'A quoted passage', quote_truncated: truncated, required: true,
  })] })] } as CitationViewerPayload;
}

describe('bounded citation quotes', () => {
  it('marks truncated viewer quotes as excerpts in the citation summary', () => {
    expect(mergeCitedPassage(payload(true))).toBe('A quoted passage… [excerpt]');
  });
  it('preserves ordinary exact citation quotes', () => {
    expect(mergeCitedPassage(payload(false))).toBe('A quoted passage');
  });
});
