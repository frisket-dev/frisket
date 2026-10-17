// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const pdf = vi.hoisted(() => ({
  layoutReady: false,
  numPages: 2,
  deferRender: false,
  textCancel: vi.fn(),
  textRender: vi.fn(),
  height: 700,
  resize: () => {},
  destroy: vi.fn(),
  getPage: vi.fn(),
  getDocument: vi.fn(),
  renderCancel: new Map<number, ReturnType<typeof vi.fn>>(),
  pageCleanup: new Map<number, ReturnType<typeof vi.fn>>(),
}));

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: {
    getDocument: pdf.getDocument,
    TextLayer: class {
      constructor({ container }: { container: HTMLElement }) {
        container.textContent = 'Selectable page text';
      }
      cancel = pdf.textCancel;
      render = pdf.textRender;
    },
  },
}));

import { PdfViewer, type DocumentPageImage } from '../../src/media/PdfViewer';

function mockDocument(numPages = pdf.numPages) {
  return { numPages, cleanup: vi.fn(), getPage: pdf.getPage };
}

function pendingDocument() {
  let resolve!: (doc: ReturnType<typeof mockDocument>) => void;
  const promise = new Promise<ReturnType<typeof mockDocument>>((complete) => { resolve = complete; });
  return { promise, resolve };
}

type ObserverCallback = (entries: IntersectionObserverEntry[]) => void;

class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = [];
  readonly observed = new Set<Element>();

  constructor(
    private readonly callback: ObserverCallback,
    readonly options?: IntersectionObserverInit,
  ) {
    MockIntersectionObserver.instances.push(this);
  }

  observe = (node: Element) => {
    this.observed.add(node);
  };

  unobserve = (node: Element) => {
    this.observed.delete(node);
  };

  disconnect = () => {
    this.observed.clear();
  };

  trigger(page: number, isIntersecting: boolean) {
    const target = [...this.observed].find(
      (node) => (node as HTMLElement).dataset.page === String(page),
    );
    if (!target) throw new Error(`Page ${page} is not observed`);
    this.callback([{ target, isIntersecting } as IntersectionObserverEntry]);
  }
}

describe('PdfViewer native page selection', () => {
  const scrollTo = vi.fn();

  beforeEach(() => {
    pdf.layoutReady = false;
    pdf.numPages = 2;
    pdf.deferRender = false;
    pdf.textCancel.mockReset();
    pdf.textRender.mockReset().mockResolvedValue(undefined);
    pdf.height = 700;
    pdf.destroy.mockReset();
    pdf.getPage.mockReset();
    pdf.getDocument.mockReset().mockImplementation(() => ({
      destroy: pdf.destroy,
      promise: Promise.resolve(mockDocument()),
    }));
    pdf.renderCancel.clear();
    pdf.pageCleanup.clear();
    pdf.getPage.mockImplementation(async (pageNumber: number) => {
      const cancel = vi.fn();
      const pageCleanup = vi.fn();
      pdf.renderCancel.set(pageNumber, cancel);
      pdf.pageCleanup.set(pageNumber, pageCleanup);
      return {
        cleanup: pageCleanup,
        getViewport: ({ scale }: { scale: number }) => ({
          width: (pageNumber % 2 === 0 ? 800 : 600) * scale,
          height: (pageNumber % 2 === 0 ? 600 : 800) * scale,
        }),
        render: () => ({
          cancel,
          promise: pdf.deferRender
            ? new Promise<void>(() => undefined)
            : Promise.resolve().then(() => {
              pdf.layoutReady = true;
            }),
        }),
        getTextContent: async () => ({ items: [] }),
      };
    });
    scrollTo.mockReset();
    MockIntersectionObserver.instances = [];
    vi.stubGlobal('IntersectionObserver', MockIntersectionObserver);
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { pdf.resize = callback; }
      observe() {}
      disconnect() {}
    });
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => pdf.height);
    vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockImplementation(function () {
      const page = Number((this as HTMLElement).dataset.page);
      if (pdf.layoutReady && page === 2) return 1000;
      return Number.isFinite(page) ? 16 + ((page - 1) * 1000) : 16;
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
    vi.useRealTimers();
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

  it('ignores a replaced file loading late and uses the latest committed callback and page', async () => {
    const oldLoad = pendingDocument();
    const newLoad = pendingDocument();
    const oldDoc = mockDocument(500);
    pdf.getDocument
      .mockReturnValueOnce({ destroy: pdf.destroy, promise: oldLoad.promise })
      .mockReturnValueOnce({ destroy: pdf.destroy, promise: newLoad.promise });
    const oldLoaded = vi.fn();
    const latestLoaded = vi.fn();
    const props = {
      layout: 'continuous' as const, fit: 'width' as const, zoom: 1, textLayer: false,
      onCurrentPageChange: vi.fn(),
    };
    const view = render(<PdfViewer {...props} url="/old.pdf" currentPage={1} onLoaded={oldLoaded} />);
    view.rerender(<PdfViewer {...props} url="/new.pdf" currentPage={1} onLoaded={oldLoaded} />);
    expect(pdf.destroy).toHaveBeenCalledOnce();
    view.rerender(<PdfViewer {...props} url="/new.pdf" currentPage={2} onLoaded={latestLoaded} />);
    await act(async () => { newLoad.resolve(mockDocument(2)); });
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(2));
    expect(latestLoaded).toHaveBeenCalledExactlyOnceWith(2);
    expect(oldLoaded).not.toHaveBeenCalled();
    expect(pdf.getDocument).toHaveBeenCalledTimes(2);
    await act(async () => { oldLoad.resolve(oldDoc); });
    expect(oldDoc.cleanup).toHaveBeenCalledOnce();
    expect(screen.getByTestId('document-pdf')).toHaveAttribute('data-page-count', '2');
    expect(screen.getAllByTestId('document-pdf-page')).toHaveLength(2);
  });

  it('clears a previous file error while its replacement is loading', async () => {
    pdf.getDocument.mockReturnValueOnce({ destroy: pdf.destroy, promise: Promise.reject(new Error('Unreadable PDF')) });
    const replacement = pendingDocument();
    pdf.getDocument.mockReturnValueOnce({ destroy: pdf.destroy, promise: replacement.promise });
    const props = {
      layout: 'continuous' as const, fit: 'width' as const, zoom: 1, textLayer: false,
      currentPage: 1, onLoaded: vi.fn(), onCurrentPageChange: vi.fn(),
    };
    const view = render(<PdfViewer {...props} url="/broken.pdf" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Unreadable PDF');
    view.rerender(<PdfViewer {...props} url="/replacement.pdf" />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryAllByTestId('document-pdf-page')).toHaveLength(0);
    await act(async () => { replacement.resolve(mockDocument(2)); });
    expect(screen.getAllByTestId('document-pdf-page')).toHaveLength(2);
  });

  it('resets measured geometry when switching files', async () => {
    const props = {
      layout: 'continuous' as const, fit: 'width' as const, zoom: 1, textLayer: false,
      onLoaded: vi.fn(), onCurrentPageChange: vi.fn(),
    };
    const view = render(<PdfViewer {...props} url="/landscape.pdf" currentPage={2} />);
    await waitFor(() => expect(screen.getAllByTestId('document-pdf-page')[1])
      .toHaveStyle({ width: '768px', height: '576px' }));
    const replacement = pendingDocument();
    pdf.getDocument.mockReturnValueOnce({ destroy: pdf.destroy, promise: replacement.promise });
    view.rerender(<PdfViewer {...props} url="/new.pdf" currentPage={1} />);
    expect(screen.queryAllByTestId('document-pdf-page')).toHaveLength(0);
    await act(async () => { replacement.resolve(mockDocument(2)); });
    const unvisitedPage = screen.getAllByTestId('document-pdf-page')[1];
    expect(unvisitedPage).toHaveAttribute('data-rendered', 'false');
    expect(unvisitedPage).toHaveStyle({ width: '768px', height: '993px' });
    expect(pdf.destroy).toHaveBeenCalledOnce();
  });

  it('clears old overscan on layout changes without reloading the document or its measured geometry', async () => {
    const props = {
      url: '/two-pages.pdf', fit: 'width' as const, zoom: 1, textLayer: false,
      currentPage: 1, onLoaded: vi.fn(), onCurrentPageChange: vi.fn(),
    };
    const view = render(<PdfViewer {...props} layout="continuous" />);
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(1));
    act(() => MockIntersectionObserver.instances.at(-1)!.trigger(2, true));
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(2));
    const landscape = screen.getAllByTestId('document-pdf-page')[1];
    expect(landscape).toHaveStyle({ width: '768px', height: '576px' });
    view.rerender(<PdfViewer {...props} layout="single" />);
    view.rerender(<PdfViewer {...props} layout="continuous" />);
    const placeholder = screen.getAllByTestId('document-pdf-page')[1];
    expect(placeholder).toHaveAttribute('data-rendered', 'false');
    expect(placeholder).toHaveStyle({ width: '768px', height: '576px' });
    expect(pdf.getDocument).toHaveBeenCalledOnce();
    expect(screen.getByTestId('document-pdf').querySelectorAll('canvas')).toHaveLength(1);
  });

  it('keeps 500 stable page anchors while mounting images only in the overscan window', async () => {
    const pages: DocumentPageImage[] = Array.from({ length: 500 }, (_, index) => ({
      page: index + 1,
      width: index === 250 ? 800 : 600,
      height: index === 250 ? 600 : 800,
      url: `/page-${index + 1}.png`,
    }));
    const view = render(
      <PdfViewer
        url="/images.pdf"
        layout="continuous"
        fit="width"
        zoom={1}
        textLayer={false}
        currentPage={250}
        onLoaded={vi.fn()}
        onCurrentPageChange={vi.fn()}
        pageImages={pages}
        totalPageCount={500}
      />,
    );

    expect(await screen.findByAltText('Page 250')).toBeInTheDocument();
    expect(screen.getAllByTestId('document-pdf-page')).toHaveLength(500);
    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(screen.getByTestId('document-pdf')).toHaveAttribute('data-page-count', '500');

    const observer = MockIntersectionObserver.instances.at(-1)!;
    expect(observer.options?.rootMargin).toBe('700px 0px');
    act(() => {
      observer.trigger(249, true);
      observer.trigger(251, true);
    });
    await waitFor(() => expect(screen.getAllByRole('img')).toHaveLength(3));

    const landscapePlaceholder = screen.getAllByTestId('document-pdf-page')[250];
    expect(landscapePlaceholder).toHaveStyle({ width: '768px', height: '576px', flexShrink: '0' });

    act(() => observer.trigger(250, false));
    view.rerender(
      <PdfViewer
        url="/images.pdf"
        layout="continuous"
        fit="width"
        zoom={1}
        textLayer={false}
        currentPage={251}
        onLoaded={vi.fn()}
        onCurrentPageChange={vi.fn()}
        pageImages={pages}
        totalPageCount={500}
      />,
    );
    await waitFor(() => expect(screen.queryByAltText('Page 250')).not.toBeInTheDocument());
    expect(landscapePlaceholder).toHaveStyle({ width: '768px', height: '576px', flexShrink: '0' });
  });

  it('cancels stale renders, cleans pages, and releases canvas backing stores', async () => {
    pdf.numPages = 500;
    pdf.deferRender = true;
    const onLoaded = vi.fn();
    const onCurrentPageChange = vi.fn();
    const view = render(
      <PdfViewer
        url="/many-pages.pdf"
        layout="continuous"
        fit="width"
        zoom={1}
        textLayer
        currentPage={250}
        onLoaded={onLoaded}
        onCurrentPageChange={onCurrentPageChange}
      />,
    );

    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(250));
    const oldCanvas = screen.getByTestId('document-pdf')
      .querySelector('[data-page="250"] canvas') as HTMLCanvasElement;
    expect(oldCanvas.width).toBeGreaterThan(0);
    expect(screen.getAllByTestId('document-pdf-textlayer')).toHaveLength(1);

    view.rerender(
      <PdfViewer
        url="/many-pages.pdf"
        layout="continuous"
        fit="width"
        zoom={1}
        textLayer
        currentPage={500}
        onLoaded={onLoaded}
        onCurrentPageChange={onCurrentPageChange}
      />,
    );

    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(500));
    expect(pdf.renderCancel.get(250)).toHaveBeenCalledOnce();
    expect(pdf.pageCleanup.get(250)).toHaveBeenCalledOnce();
    expect(oldCanvas.width).toBe(0);
    expect(oldCanvas.height).toBe(0);
    expect(screen.getAllByTestId('document-pdf-textlayer')).toHaveLength(1);
    expect(screen.getAllByTestId('document-pdf-page')).toHaveLength(500);
  });

  it('does not turn a scroll-reported page into a programmatic jump', async () => {
    vi.useFakeTimers();
    pdf.numPages = 3;
    const onCurrentPageChange = vi.fn();
    const onLoaded = vi.fn();
    const view = render(
      <PdfViewer
        url="/three-pages.pdf"
        layout="continuous"
        fit="width"
        zoom={1}
        textLayer={false}
        currentPage={1}
        onLoaded={onLoaded}
        onCurrentPageChange={onCurrentPageChange}
      />,
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => vi.advanceTimersByTime(121));
    scrollTo.mockClear();

    const container = screen.getByTestId('document-pdf');
    Object.defineProperty(container, 'scrollTop', { configurable: true, value: 1000 });
    fireEvent.scroll(container);
    expect(onCurrentPageChange).toHaveBeenCalledWith(2);

    view.rerender(
      <PdfViewer
        url="/three-pages.pdf"
        layout="continuous"
        fit="width"
        zoom={1}
        textLayer={false}
        currentPage={2}
        onLoaded={onLoaded}
        onCurrentPageChange={onCurrentPageChange}
      />,
    );
    expect(scrollTo).not.toHaveBeenCalled();
  });


  it('bounds native canvases and selectable text, preserves mixed page geometry, and returns after zoom', async () => {
    pdf.numPages = 500;
    const props = {
      url: '/many-pages.pdf',
      layout: 'continuous' as const,
      fit: 'width' as const,
      zoom: 1,
      textLayer: true,
      onLoaded: vi.fn(),
      onCurrentPageChange: vi.fn(),
    };
    const view = render(<PdfViewer {...props} currentPage={250} />);
    await waitFor(() => expect(pdf.textRender).toHaveBeenCalledOnce());
    const container = screen.getByTestId('document-pdf');
    expect(container.querySelectorAll('canvas')).toHaveLength(1);
    expect(pdf.getPage.mock.calls.map(([page]) => page)).toEqual([250]);
    const landscape = container.querySelector('[data-page="250"]')!;
    expect(landscape).toHaveStyle({ width: '768px', height: '576px', flexShrink: '0' });
    expect(landscape.querySelector('.textLayer')).toHaveTextContent('Selectable page text');

    const observer = MockIntersectionObserver.instances.at(-1)!;
    act(() => {
      observer.trigger(249, true);
      observer.trigger(251, true);
    });
    await waitFor(() => expect(pdf.textRender).toHaveBeenCalledTimes(3));
    expect(container.querySelectorAll('canvas')).toHaveLength(3);
    act(() => {
      observer.trigger(249, false);
      observer.trigger(251, false);
    });
    view.rerender(<PdfViewer {...props} currentPage={500} />);
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(500));
    expect(landscape).toHaveAttribute('data-rendered', 'false');
    expect(landscape).toHaveStyle({ width: '768px', height: '576px' });
    expect(landscape.querySelector('canvas')).toBeNull();
    expect(landscape.querySelector('.textLayer')).toBeNull();
    expect(pdf.textCancel).toHaveBeenCalledTimes(3);
    expect(MockIntersectionObserver.instances.at(-1)).toBe(observer);

    scrollTo.mockClear();
    view.rerender(<PdfViewer {...props} currentPage={250} zoom={2} />);
    await waitFor(() => expect(landscape.querySelector('canvas')?.width).toBe(1536));
    expect(landscape).toHaveStyle({ width: '1536px', height: '1152px' });
    expect(scrollTo).toHaveBeenCalledWith({ top: 249000, behavior: 'auto' });
    expect(container.querySelectorAll('canvas')).toHaveLength(1);
    await waitFor(() => expect(landscape.querySelector('.textLayer')).toHaveTextContent('Selectable page text'));
  });

  it('does not redraw width-fit pages when only the container height changes', async () => {
    render(
      <PdfViewer url="/two-pages.pdf" layout="continuous" fit="width" zoom={1}
        textLayer currentPage={1} onLoaded={vi.fn()} onCurrentPageChange={vi.fn()} />,
    );
    await waitFor(() => expect(pdf.textRender).toHaveBeenCalledOnce());
    pdf.height = 900;
    act(() => pdf.resize());
    expect(pdf.getPage).toHaveBeenCalledOnce();
    expect(pdf.textCancel).not.toHaveBeenCalled();
  });

  it('cancels an in-flight text layer when its page leaves the viewport', async () => {
    pdf.textRender.mockImplementation(() => new Promise<void>(() => undefined));
    const props = {
      url: '/two-pages.pdf', layout: 'continuous' as const, fit: 'width' as const,
      zoom: 1, textLayer: true, onLoaded: vi.fn(), onCurrentPageChange: vi.fn(),
    };
    const view = render(<PdfViewer {...props} currentPage={1} />);
    await waitFor(() => expect(pdf.textRender).toHaveBeenCalledOnce());
    const oldText = screen.getByTestId('document-pdf-textlayer');
    view.rerender(<PdfViewer {...props} currentPage={2} />);
    await waitFor(() => expect(pdf.textCancel).toHaveBeenCalledOnce());
    expect(oldText).toBeEmptyDOMElement();
    expect(oldText).not.toBeInTheDocument();
  });

  it('keeps single and two-up layouts fully rendered', async () => {
    const pages: DocumentPageImage[] = Array.from({ length: 4 }, (_, index) => ({
      page: index + 1,
      width: 600,
      height: 800,
      url: `/page-${index + 1}.png`,
    }));
    const props = {
      url: '/images.pdf',
      fit: 'page' as const,
      zoom: 1,
      textLayer: false,
      onLoaded: vi.fn(),
      onCurrentPageChange: vi.fn(),
      pageImages: pages,
      totalPageCount: 4,
    };
    const view = render(<PdfViewer {...props} layout="single" currentPage={3} />);
    expect(await screen.findByAltText('Page 3')).toBeInTheDocument();
    expect(screen.getAllByTestId('document-pdf-page')).toHaveLength(1);

    view.rerender(<PdfViewer {...props} layout="two-up" currentPage={4} />);
    expect(await screen.findByAltText('Page 4')).toBeInTheDocument();
    expect(screen.getByAltText('Page 3')).toBeInTheDocument();
    expect(screen.getAllByTestId('document-pdf-page')).toHaveLength(2);
  });
});
