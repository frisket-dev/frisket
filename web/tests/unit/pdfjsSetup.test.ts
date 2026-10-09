import { beforeEach, describe, expect, it, vi } from 'vitest';

const pdfjs = vi.hoisted(() => ({
  getDocument: vi.fn(() => ({ promise: Promise.resolve({}) })),
  GlobalWorkerOptions: { workerSrc: '' },
}));

vi.mock('pdfjs-dist', () => ({
  ...pdfjs,
  TextLayer: class {},
}));
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/assets/pdf.worker.js' }));
vi.mock('pdfjs-dist/web/pdf_viewer.css', () => ({}));

import { pdfjsLib, pdfjsWasmUrl } from '../../src/media/pdfjsSetup';

describe('PDF.js runtime assets', () => {
  beforeEach(() => pdfjs.getDocument.mockClear());

  it('loads every document with the locally bundled decoder directory', () => {
    pdfjsLib.getDocument({ url: '/document.pdf' });

    expect(pdfjsWasmUrl).toBe('/pdfjs/wasm/');
    expect(pdfjs.GlobalWorkerOptions.workerSrc).toBe('/assets/pdf.worker.js');
    expect(pdfjs.getDocument).toHaveBeenCalledWith({
      url: '/document.pdf',
      wasmUrl: '/pdfjs/wasm/',
    });
  });

  it('does not let a caller route decoder requests to an external directory', () => {
    pdfjsLib.getDocument({ url: '/document.pdf', wasmUrl: 'https://example.invalid/pdfjs/' });

    expect(pdfjs.getDocument).toHaveBeenCalledWith({
      url: '/document.pdf',
      wasmUrl: '/pdfjs/wasm/',
    });
  });
});
