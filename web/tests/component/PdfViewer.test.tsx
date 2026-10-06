// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const pdf = vi.hoisted(() => ({
  layoutReady: false,
  destroy: vi.fn(),
}));

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: {
    getDocument: () => ({
      destroy: pdf.destroy,
      promise: Promise.resolve({
        numPages: 2,
        cleanup: vi.fn(),
        getPage: async () => ({
          cleanup: vi.fn(),
          getViewport: ({ scale }: { scale: number }) => ({
            width: 600 * scale,
            height: 800 * scale,
          }),
          render: () => ({
            cancel: vi.fn(),
            promise: Promise.resolve().then(() => {
              pdf.layoutReady = true;
            }),
          }),
        }),
      }),
    }),
    TextLayer: class {},
  },
}));

import { PdfViewer } from '../../src/media/PdfViewer';

describe('PdfViewer native page selection', () => {
  const scrollTo = vi.fn();

  beforeEach(() => {
    pdf.layoutReady = false;
    scrollTo.mockReset();
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      disconnect() {}
    });
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(700);
    vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockImplementation(function () {
      const page = (this as HTMLElement).dataset.page;
      return pdf.layoutReady && page === '2' ? 1000 : 16;
    });
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
      configurable: true,
      value: scrollTo,
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('repositions after async canvas layout makes the selected page measurable', async () => {
    render(
      <PdfViewer
        url="/two-pages.pdf"
        layout="continuous"
        fit="width"
        zoom={1}
        textLayer={false}
        currentPage={2}
        onLoaded={vi.fn()}
        onCurrentPageChange={vi.fn()}
        pageId={(page) => `page-${page}`}
        renderPageOverlay={(page) => page === 2 ? <span data-testid="overlay" /> : null}
      />,
    );

    expect(await screen.findByTestId('overlay')).toBeInTheDocument();
    expect(document.getElementById('page-2')).toHaveAttribute('data-page', '2');
    await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({ top: 984, behavior: 'auto' }));
  });
});
