// pdf.js, self-hosted. The repo rule is NO external fetch (the tokens spec pins
// the no-CDN precedent), so the worker is imported through Vite's `?url` so it
// is fingerprinted, bundled, and served same-origin from our own build — never
// from a CDN. Importing this module has the side effect of wiring the worker.
import * as pdfjsModule from 'pdfjs-dist';
// The bundled worker (ESM build). Vite rewrites this to a hashed same-origin URL.
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
// pdf.js's own text-layer stylesheet (selectable-text positioning). Bundled by
// Vite — no network request.
import 'pdfjs-dist/web/pdf_viewer.css';

pdfjsModule.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

// PDF.js 5+ loads JPEG 2000, JBIG2, and color-management decoders by filename
// from a shared base directory. Vite serves this directory in development and
// copies the installed pdfjs-dist runtime files into every production edition.
export const pdfjsWasmUrl = `${import.meta.env.BASE_URL}pdfjs/wasm/`;

type PdfDocumentParameters = NonNullable<Parameters<typeof pdfjsModule.getDocument>[0]>;

function getDocument(parameters: PdfDocumentParameters) {
  return pdfjsModule.getDocument({
    ...parameters,
    wasmUrl: pdfjsWasmUrl,
  });
}

// Keep PDF.js access behind this configured facade so every current and future
// consumer receives the same worker and decoder asset setup.
export const pdfjsLib = {
  ...pdfjsModule,
  getDocument,
};

export type PdfDocumentProxy = Awaited<ReturnType<typeof pdfjsLib.getDocument>['promise']>;
export type PdfPageProxy = Awaited<ReturnType<PdfDocumentProxy['getPage']>>;
