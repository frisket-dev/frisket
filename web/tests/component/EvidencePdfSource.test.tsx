// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ArtifactSource } from '../../src/components/EvidenceViewer';
import type { EvidenceArtifact } from '../../src/api/types';
import { artifactRef, blobRef, evidenceArtifact } from '../support/evidenceFixtures';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function installIntersectionObserver() {
  const observations: Array<{
    target: Element;
    callback: IntersectionObserverCallback;
    observer: IntersectionObserver;
  }> = [];
  class FakeIntersectionObserver {
    readonly root = null;
    readonly rootMargin = '240px 0px';
    readonly thresholds = [0];
    constructor(private readonly callback: IntersectionObserverCallback) {}
    observe = (target: Element) => observations.push({
      target,
      callback: this.callback,
      observer: this as unknown as IntersectionObserver,
    });
    disconnect() {}
    unobserve() {}
    takeRecords(): IntersectionObserverEntry[] { return []; }
  }
  vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
  return observations;
}

describe('evidence PDF renderer', () => {
  it('starts page images only as their retained page anchors become visible', async () => {
    const observations = installIntersectionObserver();
    const pages = Array.from({ length: 40 }, (_, index) => ({
      page: index + 1,
      image: null,
      render_url: `/api/projects/p/blobs/pdf/pages/${index + 1}/image`,
      text: null,
      regions: [],
    })) as EvidenceArtifact['pages'];
    const artifact = evidenceArtifact({ media_type: 'application/pdf', pages });

    render(<ArtifactSource artifact={artifact} />);

    expect(screen.getAllByTestId('evidence-page')).toHaveLength(40);
    expect(screen.queryByTestId('evidence-page-image')).toBeNull();
    expect(observations).toHaveLength(40);

    for (const observed of [observations[0], observations[23]]) {
      act(() => observed.callback([{
        target: observed.target,
        isIntersecting: true,
      } as IntersectionObserverEntry], observed.observer));
    }
    await waitFor(() => expect(screen.getAllByTestId('evidence-page-image')).toHaveLength(2));
    expect(screen.getAllByTestId('evidence-page-image').map((image) => image.getAttribute('src'))).toEqual([
      '/api/projects/p/blobs/pdf/pages/1/image',
      '/api/projects/p/blobs/pdf/pages/24/image',
    ]);
    expect(document.getElementById(`evidence-page-${artifact.stable_id}-24`)).not.toBeNull();

    const firstImage = screen.getAllByTestId('evidence-page-image')[0];
    vi.spyOn(firstImage, 'getBoundingClientRect').mockReturnValue({ height: 640 } as DOMRect);
    fireEvent.load(firstImage);
    act(() => observations[0].callback([{
      target: observations[0].target,
      isIntersecting: false,
    } as IntersectionObserverEntry], observations[0].observer));
    expect(screen.getAllByTestId('evidence-page-image')).toHaveLength(1);
    expect(observations[0].target).toHaveStyle({ minHeight: '640px' });
  });

  it('shows a bounded page error when lazy rendering fails', async () => {
    const observations = installIntersectionObserver();
    const artifact = evidenceArtifact({
      media_type: 'application/pdf',
      pages: [{ page: 1, image: null, render_url: '/page/1', text: null, regions: [] }] as EvidenceArtifact['pages'],
    });
    render(<ArtifactSource artifact={artifact} />);
    act(() => observations[0].callback([{
      target: observations[0].target,
      isIntersecting: true,
    } as IntersectionObserverEntry], observations[0].observer));

    const image = await screen.findByTestId('evidence-page-image');
    fireEvent.error(image);
    expect(screen.getByTestId('evidence-page-image-error')).toHaveTextContent('Could not render page 1.');
    expect(screen.queryByTestId('evidence-region-highlight')).toBeNull();
  });

  it('continues to render legacy persisted PNG page evidence', async () => {
    const artifact = evidenceArtifact({
      media_type: 'application/pdf',
      pages: [{
        page: 1,
        image: { blob_hash: 'png', url: '/page.png', width: 10, height: 10 },
        text: null,
        regions: [],
      }],
    });
    render(<ArtifactSource artifact={artifact} />);
    expect(await screen.findByTestId('evidence-page-image')).toHaveAttribute('src', '/page.png');
  });

  it('renders the original PDF blob instead of the unsupported-artifact fallback', () => {
    const artifact = evidenceArtifact({
      artifact_kind: 'file',
      media_type: 'application/pdf',
      filename: 'filing.pdf',
      artifact_ref: artifactRef({
        artifact_kind: 'file',
        media_type: 'application/pdf',
        blob: blobRef('/api/projects/p/blobs/pdf', { filename: 'filing.pdf' }),
      }),
    });

    render(<ArtifactSource artifact={artifact} />);

    expect(screen.getByTestId('evidence-pdf')).toHaveAttribute(
      'src',
      '/api/projects/p/blobs/pdf',
    );
    expect(screen.queryByTestId('evidence-artifact-fallback')).toBeNull();
  });
});
