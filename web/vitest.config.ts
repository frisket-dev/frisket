import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// core/ and state/ are framework-free (no react/react-dom) and run under the
// default `node` environment; no jsdom needed there.
//
// Component/unit tests co-located under src/ cover focused widget, renderer,
// and hook behavior without booting the full e2e stack. React-rendering
// files opt into jsdom per-file with a `// @vitest-environment jsdom` docblock
// so the framework-free suites keep the fast node environment.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'node',
    include: [
      'src/core/**/*.test.ts',
      'src/state/**/*.test.ts',
      // Focused widget, renderer, and hook tests stay outside src/ so the
      // production tsc build, dependency-cruiser boundary
      // scan, and react-doctor gate (all scoped to src/) are untouched; vitest
      // transpiles + runs them here.
      'tests/component/**/*.test.{ts,tsx}',
      'tests/unit/**/*.test.{ts,tsx}',
    ],
    setupFiles: ['./vitest.setup.ts'],
  },
});
