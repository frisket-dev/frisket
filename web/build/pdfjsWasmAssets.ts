import { createReadStream, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';

export const PDFJS_WASM_PUBLIC_PATH = '/pdfjs/wasm/';

const REQUIRED_RUNTIME_FILES = new Set([
  'jbig2.wasm',
  'jbig2_nowasm_fallback.js',
  'openjpeg.wasm',
  'openjpeg_nowasm_fallback.js',
  'qcms_bg.wasm',
]);

export interface PdfjsWasmAsset {
  filename: string;
  sourcePath: string;
}

export function pdfjsWasmAssetsFrom(sourceDirectory: string): PdfjsWasmAsset[] {
  const assets = readdirSync(sourceDirectory, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => ({
      filename: entry.name,
      sourcePath: join(sourceDirectory, entry.name),
    }));
  const available = new Set(assets.map((asset) => asset.filename));
  const missing = [...REQUIRED_RUNTIME_FILES].filter((filename) => !available.has(filename));
  if (missing.length) {
    throw new Error(`pdfjs-dist is missing required WASM runtime files: ${missing.join(', ')}`);
  }
  return assets;
}

function contentType(filename: string): string {
  switch (extname(filename)) {
    case '.wasm':
      return 'application/wasm';
    case '.js':
      return 'text/javascript; charset=utf-8';
    default:
      return 'text/plain; charset=utf-8';
  }
}

/** Bundle and dev-serve the runtime files shipped with the installed PDF.js. */
export function pdfjsWasmAssets(sourceDirectory = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../node_modules/pdfjs-dist/wasm',
)): Plugin {
  let assets = pdfjsWasmAssetsFrom(sourceDirectory);
  return {
    name: 'frisket-pdfjs-wasm-assets',
    configResolved() {
      assets = pdfjsWasmAssetsFrom(sourceDirectory);
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const requestPath = new URL(request.url ?? '/', 'http://frisket.local').pathname;
        if (!requestPath.startsWith(PDFJS_WASM_PUBLIC_PATH)) {
          next();
          return;
        }
        const filename = decodeURIComponent(requestPath.slice(PDFJS_WASM_PUBLIC_PATH.length));
        const asset = assets.find((candidate) => candidate.filename === filename);
        if (!asset || filename.includes(sep) || !statSync(asset.sourcePath).isFile()) {
          next();
          return;
        }
        response.statusCode = 200;
        response.setHeader('Content-Type', contentType(filename));
        response.setHeader('Cache-Control', 'no-cache');
        createReadStream(asset.sourcePath).pipe(response);
      });
    },
    generateBundle() {
      for (const asset of assets) {
        this.emitFile({
          type: 'asset',
          fileName: `pdfjs/wasm/${asset.filename}`,
          source: readFileSync(asset.sourcePath),
        });
      }
    },
  };
}
