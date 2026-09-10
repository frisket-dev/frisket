// pdf.js, self-hosted. The repo rule is NO external fetch (the tokens spec pins
// the no-CDN precedent), so the worker is imported through Vite's `?url` so it
// is fingerprinted, bundled, and served same-origin from our own build — never
// from a CDN. Importing this module has the side effect of wiring the worker.
import * as pdfjsLib from 'pdfjs-dist';
// The bundled worker (ESM build). Vite rewrites this to a hashed same-origin URL.
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
// pdf.js's own text-layer stylesheet (selectable-text positioning). Bundled by
// Vite — no network request.
import 'pdfjs-dist/web/pdf_viewer.css';

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export { pdfjsLib };
export type PdfDocumentProxy = Awaited<ReturnType<typeof pdfjsLib.getDocument>['promise']>;
export type PdfPageProxy = Awaited<ReturnType<PdfDocumentProxy['getPage']>>;
