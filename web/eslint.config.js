import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', 'test-results']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
  },
  // Boundary rules for the workspace state substrate: core/ is framework-free.
  {
    files: ['src/core/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': ['error', {
        paths: [
          { name: 'react', message: 'core/ is framework-free; put React adapters in bind/.' },
          { name: 'react-dom', message: 'core/ is framework-free.' },
        ],
        patterns: [
          { group: ['*/state/*', '*/bind/*', '**/App*', '**/workspace/*', '**/workbench/*'],
            message: 'core/ must not import app layers.' },
        ],
      }],
    },
  },
  // state/ imports core/ only (+ api types), never bind/ or React, and never
  // sibling stores — the structural enforcement of "never one mega-handle".
  {
    files: ['src/state/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': ['error', {
        patterns: [
          { group: ['react', 'react-dom', '*/bind/*', '**/App*'],
            message: 'state/ is React-free and app-free.' },
          { group: ['*/state/*'],
            message: 'stores must not import sibling stores; compose in bind/ or selectors.' },
        ],
      }],
    },
  },
  // HTTP-07: browser transport is an explicit source boundary. Route application
  // requests through api/httpContract.ts; only the named blob/auth/Arrow adapters
  // retain direct transport access for their browser integration work.
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: [
      'src/api/httpContract.ts',
      'src/api/raw/browserAuth.ts',
      'src/api/raw/blobText.ts',
      'src/api/raw/mapPointsArrow.ts',
      '**/*.{test,spec}.{ts,tsx}',
      '**/__tests__/**',
      '**/tests/**',
    ],
    rules: {
      'no-restricted-syntax': ['error',
        {
          selector: "CallExpression[callee.type='Identifier'][callee.name='fetch']",
          message: 'Use api/httpContract.ts instead of calling fetch directly.',
        },
        {
          selector: "CallExpression[callee.type='MemberExpression'][callee.computed=false][callee.property.type='Identifier'][callee.property.name='fetch']",
          message: 'Use api/httpContract.ts instead of calling fetch directly.',
        },
        {
          selector: "CallExpression[callee.type='MemberExpression'][callee.computed=true][callee.property.type='Literal'][callee.property.value='fetch']",
          message: 'Use api/httpContract.ts instead of calling fetch directly.',
        },
        {
          selector: "CallExpression[callee.type='Identifier'][callee.name='sendBeacon']",
          message: 'Keep beacon transport behind an approved API adapter.',
        },
        {
          selector: "CallExpression[callee.type='MemberExpression'][callee.computed=false][callee.property.type='Identifier'][callee.property.name='sendBeacon']",
          message: 'Keep beacon transport behind an approved API adapter.',
        },
        {
          selector: "CallExpression[callee.type='MemberExpression'][callee.computed=true][callee.property.type='Literal'][callee.property.value='sendBeacon']",
          message: 'Keep beacon transport behind an approved API adapter.',
        },
        {
          selector: "NewExpression[callee.type='Identifier'][callee.name=/^(XMLHttpRequest|EventSource|WebSocket)$/]",
          message: 'Keep browser transport constructors behind an approved API adapter.',
        },
        {
          selector: "NewExpression[callee.type='MemberExpression'][callee.computed=false][callee.property.type='Identifier'][callee.property.name=/^(XMLHttpRequest|EventSource|WebSocket)$/]",
          message: 'Keep browser transport constructors behind an approved API adapter.',
        },
        {
          selector: "NewExpression[callee.type='MemberExpression'][callee.computed=true][callee.property.type='Literal'][callee.property.value=/^(XMLHttpRequest|EventSource|WebSocket)$/]",
          message: 'Keep browser transport constructors behind an approved API adapter.',
        },
      ],
    },
  },
])
