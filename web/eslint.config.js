import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

const TRANSPORT_SELECTORS = [
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
  // Provenance URL fields carry strings recorded from scraped or imported
  // material; React renders a `javascript:` href as-is.
  {
    selector: "JSXAttribute[name.name=/^(href|src)$/] :matches(JSXExpressionContainer > MemberExpression, JSXExpressionContainer > ChainExpression > MemberExpression)[property.name=/^(source_url|canonical_url|deep_link_url|webpage_url)$/]",
    message: 'Untrusted provenance URL: pass it through safeHttpUrl (core/safeUrl.ts) and render text when it returns null.',
  },
]

const FEATURE_FOLDERS = [
  'src/components/**/*.{ts,tsx}',
  'src/workbench/**/*.{ts,tsx}',
  'src/settings/**/*.{ts,tsx}',
  'src/engine-selector/**/*.{ts,tsx}',
  'src/actions/**/*.{ts,tsx}',
]
const TEST_FILES = ['**/*.{test,spec}.{ts,tsx}', '**/__tests__/**', '**/tests/**']

const LIVENESS_FLAG = {
  selector: "CallExpression[callee.name=/^use(Layout)?Effect$/] > ArrowFunctionExpression VariableDeclaration[kind='let'] > VariableDeclarator[id.name=/^(alive|cancelled|canceled|active|current|live|mounted|disposed|ignore|stale)$/][init.type='Literal']",
  message: 'Load data with useLoader (hooks/useLoader); it drops stale and post-unmount responses.',
}
const ERROR_TERNARY = {
  selector: "ConditionalExpression[test.operator='instanceof'][test.right.name='Error'][consequent.type='MemberExpression'][consequent.property.name='message']",
  message: 'Use errorMessage(error, fallback) from hooks/errorMessage.',
}
const PROP_STUB = {
  selector: "JSXAttribute > JSXExpressionContainer > ArrowFunctionExpression[params.length=0][body.type='Identifier'][body.name='undefined']",
  message: 'Make the prop optional instead of passing a () => undefined stub.',
}

// Shrink-only: each keeps a liveness-flag effect the shared hooks do not express.
const KEEPS_LIVENESS_FLAG = [
  'src/components/ActionPanel.tsx', // open-intent effect guards StrictMode with a ref and a microtask
  'src/components/ColumnDrawer.tsx', // stats refetch takes arguments; the runs list paginates
  'src/components/CitationViewer.tsx', // locate-current request shares an epoch guard and recovery state
  'src/components/FriendlyFiltersPanel.tsx', // two-level freshness plus a debounced search
  'src/components/MentionsPanel.tsx', // reducer state with sequence-guarded page appends
  'src/components/ReplayModeBanner.tsx', // runtime-config subscription
  'src/components/SignIn.tsx', // config load writes the same error the mutations clear
  'src/components/SkillsSettings.tsx', // load seeds an editable draft; writes are generation-guarded
  'src/components/TopNav.tsx', // OAuth connections load seeds the editable connection choice
  'src/components/action-panel/GeneratedActionForm.tsx', // debounced validation and output resolution
  'src/components/evidence/CitationDocumentSource.tsx', // IntersectionObserver subscription
  'src/components/importWorkspace/tableImport/TableImportFlow.tsx', // generation-guarded draft lifecycle
  'src/components/pdfSplit/PdfSplitSetup.tsx', // admission effect with delete-on-abort cleanup
  'src/components/project-ask/AskSourcePicker.tsx', // load-more appends pages
  'src/components/review/ReapplyResultsDialog.tsx', // sheets load seeds the editable output names
  'src/components/review/useReviewRuns.ts', // seeds the selection; load-more appends
  'src/components/rowInspection/CellCitations.tsx', // payload state shared with the audit refetch
  'src/settings/ProjectIntegrationSettings.tsx', // per-plugin partial failures with functional local updates
  'src/workbench/AnswersView.tsx', // appending page list
  'src/workbench/ImageGallery.tsx', // appending page list
  'src/workbench/LineagePanel.tsx', // polling that also refreshes sheets
  'src/workbench/MentionDetailPanel.tsx', // load-more and per-row occurrence appends
  'src/workbench/evaluation/ComparisonEvaluationWorkspace.tsx', // writes the parent hook's shared error
  'src/workbench/evaluation/useComparisonEvaluation.ts', // shared error plus locally patched lists
  'src/workbench/useDocumentView.ts', // generation-guarded cursor pagination
]
// Shrink-only: each passes () => undefined for a required callback prop.
const KEEPS_PROP_STUB = [
  'src/components/action-panel/GeneratedActionForm.tsx', // params-only editor reuses the launcher form
  'src/engine-selector/SelectorField.tsx', // read-only selector has no change handler
  'src/workbench/extract/ExtractView.tsx', // page viewer without the reader chrome
  'src/workbench/useOcrVariantConfigure.tsx', // page viewer without the reader chrome
]

export default defineConfig([
  globalIgnores(['dist', 'storybook-static', 'test-results']),
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
      'src/api/raw/cellValue.ts',
      'src/api/raw/mapPointsArrow.ts',
      'src/api/raw/skillMarkdownUpload.ts',
      'src/api/raw/evaluationDownloads.ts',
      '**/*.{test,spec}.{ts,tsx}',
      '**/__tests__/**',
      '**/tests/**',
    ],
    rules: {
      'no-restricted-syntax': ['error', ...TRANSPORT_SELECTORS],
    },
  },
  // Feature folders load data through hooks/useLoader and run mutations
  // through hooks/useAction. ESLint replaces a rule's options per file, so each
  // block repeats the transport selectors; the shrink-only lists below drop
  // only the selector a file still violates on purpose.
  {
    files: FEATURE_FOLDERS,
    ignores: TEST_FILES,
    rules: { 'no-restricted-syntax': ['error', ...TRANSPORT_SELECTORS, LIVENESS_FLAG, ERROR_TERNARY, PROP_STUB] },
  },
  {
    files: KEEPS_LIVENESS_FLAG.filter((file) => !KEEPS_PROP_STUB.includes(file)),
    rules: { 'no-restricted-syntax': ['error', ...TRANSPORT_SELECTORS, ERROR_TERNARY, PROP_STUB] },
  },
  {
    files: KEEPS_PROP_STUB.filter((file) => !KEEPS_LIVENESS_FLAG.includes(file)),
    rules: { 'no-restricted-syntax': ['error', ...TRANSPORT_SELECTORS, LIVENESS_FLAG, ERROR_TERNARY] },
  },
  {
    files: KEEPS_LIVENESS_FLAG.filter((file) => KEEPS_PROP_STUB.includes(file)),
    rules: { 'no-restricted-syntax': ['error', ...TRANSPORT_SELECTORS, ERROR_TERNARY] },
  },
])
