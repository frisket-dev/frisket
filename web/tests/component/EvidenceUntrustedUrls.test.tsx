/** @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ArtifactDetails } from '../../src/components/evidence/EvidenceDetails';
import { EvidenceMediaSource } from '../../src/components/evidence/EvidenceMediaSource';
import { artifactRef, blobRef, evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

beforeEach(() => { Element.prototype.scrollIntoView = vi.fn(); });
afterEach(cleanup);

const SCRIPT_URL = 'javascript:alert(document.domain)';
const PAGE_URL = 'https://video.example.test/watch/42';

function hrefs(container: HTMLElement): (string | null)[] {
  return [...container.querySelectorAll('a')].map((anchor) => anchor.getAttribute('href'));
}

describe('evidence provenance URLs', () => {
  it.each([[PAGE_URL, [PAGE_URL]], [SCRIPT_URL, []]])(
    'links an artifact source URL only when it is http(s): %s', (sourceUrl, expected) => {
      const { container } = render(<ArtifactDetails artifact={evidenceArtifact({ source_url: sourceUrl })} />);
      expect(container).toHaveTextContent(sourceUrl);
      expect(hrefs(container)).toEqual(expected);
    },
  );

  it.each([[PAGE_URL, [PAGE_URL]], [SCRIPT_URL, []]])(
    'offers the no-preview source link only for an http(s) URL: %s', (sourceUrl, expected) => {
      const artifact = evidenceArtifact({ media_type: 'text/html', source_url: sourceUrl });
      const { container } = render(<EvidenceMediaSource artifact={artifact} emphasizedSpanIds={[]} />);
      expect(screen.getByTestId('evidence-artifact-fallback')).toBeInTheDocument();
      expect(hrefs(container)).toEqual(expected);
    },
  );

  it.each([[PAGE_URL, true], [SCRIPT_URL, false]])(
    'renders timestamp deep links only for an http(s) URL: %s', (deepLink, linked) => {
      const artifact = evidenceArtifact({
        media_type: 'video/mp4',
        artifact_ref: artifactRef({ blob: blobRef('/api/projects/p/blobs/clip') }),
        spans: [evidenceSpan({
          span_kind: 'temporal', selector: { start_ms: 1000, end_ms: 2000 }, quote: 'Spoken line', deep_link_url: deepLink,
        })],
      });
      render(<EvidenceMediaSource artifact={artifact} emphasizedSpanIds={[]} />);
      expect(screen.getByText('Spoken line', { exact: false })).toBeInTheDocument();
      const links = [
        ...screen.queryAllByTestId('evidence-temporal-run-open'),
        ...screen.queryAllByTestId('evidence-temporal-deep-link'),
      ];
      expect(links.map((link) => link.getAttribute('href'))).toEqual(linked ? [deepLink, deepLink] : []);
    },
  );
});
