#!/usr/bin/env node
// web/scripts/check-substrate-boundaries.mjs
//
// Belt-and-suspenders ledger check for the "bind/ is the only React-facing
// layer" boundary rule. ESLint's no-restricted-imports (eslint.config.js) can be
// defeated by re-exports; this grep-style check catches the symptom
// directly: no core/ or state/ file may NAME a React hook, imported or not.
//
// Fails (exit 1) if any core/ or state/ file references a React hook name.

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const scanRoots = ['src/core', 'src/state'];
const hookPattern = /\buse(State|Effect|Reducer|Ref|Memo|Callback|Context|SyncExternalStore)\b/;

function* walk(dir) {
  let entries;
  try {
    entries = readdirSync(dir);
  } catch (err) {
    if (err.code === 'ENOENT') return; // scanRoot not created yet — fine, no violations
    throw err;
  }
  for (const entry of entries) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      yield* walk(full);
    } else if (/\.(ts|tsx)$/.test(entry)) {
      yield full;
    }
  }
}

const violations = [];
for (const root of scanRoots) {
  for (const file of walk(join(webRoot, root))) {
    const source = readFileSync(file, 'utf8');
    const lines = source.split('\n');
    for (let i = 0; i < lines.length; i += 1) {
      const match = lines[i].match(hookPattern);
      if (match) {
        violations.push(`${file.slice(webRoot.length + 1)}:${i + 1}: ${match[0]}`);
      }
    }
  }
}

if (violations.length > 0) {
  console.error('substrate boundary violation: core/ and state/ must not name a React hook.');
  for (const v of violations) console.error(`  ${v}`);
  process.exit(1);
}


// Route writes have single ownership. Mounting createRouteSyncController while
// direct navigate()/replaceRoute() write sites survive would double-write
// history, so the invariant is enforced from both ends:
//   (a) POSITIVE — the controller MUST be mounted in bind/ (it is the single
//       workspace-route writer). Its absence is a regression.
//   (b) NEGATIVE — no DIRECT workspace-route write may survive: every
//       navigate()/replaceRoute() in the route-owning files (useWorkspaceModel
//       + App) must be the controller's write adapter (commitRoute) or an
//       ALLOWLISTED app-shell navigation. These cannot double-write, but NOT
//       because none is RouteState-representable — a bare project route IS
//       ({ projectId, sheetId: null }). They are safe structurally: picker/
//       settings navigate OUT of the workspace; project-open fires from the
//       HomeScreen before the workspace controller mounts (a project switch
//       unmounts/remounts it); and the controller writes only on routeStore
//       mutations and never listens to popstate, so it cannot echo them.
//       review-close's raw window.history.back() is the other carve-out because
//       a store mutation cannot pop the stack.
{
  const { readFileSync: read } = await import('node:fs');

  // (a) The controller must be imported+mounted in bind/.
  const mount = read(join(webRoot, 'src/bind/useRouteSyncController.ts'), 'utf8');
  if (!/createRouteSyncController\(/.test(mount)) {
    console.error('check-substrate-boundaries: bind/useRouteSyncController.ts must mount createRouteSyncController (single workspace-route writer).');
    process.exit(1);
  }
  const model = read(join(webRoot, 'src/workspace/useWorkspaceModel.tsx'), 'utf8');
  if ((model.match(/useRouteSyncController\(/g) ?? []).length !== 1) {
    console.error('check-substrate-boundaries: the RouteSyncController must be mounted exactly once in useWorkspaceModel.');
    process.exit(1);
  }

  // (b) No direct workspace-route write outside the adapter + allowlist.
  const ROUTE_WRITE_ALLOWLIST = {
    'src/workspace/useWorkspaceModel.tsx': [
      // The controller's single write adapter (commitRoute → navigate/replaceRoute).
      "if (mode === 'replace') replaceRoute(nextRoute);",
      'else navigate(nextRoute);',
      // App-shell: Settings navigates OUT of the workspace (not a project route).
      "navigate({ kind: 'settings', projectId: project.id, scope: 'project', section: 'general' });",
      "navigate({ kind: 'settings', projectId: project.id, scope: 'project', section: 'notifications' });",
    ],
    'src/App.tsx': [
      // App-shell: project-open fires from the HomeScreen before the workspace
      // controller mounts (a project switch unmounts/remounts it); picker leaves
      // the workspace. The controller never listens to popstate, so neither can
      // double-write — even though a bare project route IS RouteState-representable.
      "<HomeScreen onOpen={(p) => navigate({ kind: 'project', projectId: p.id })} />",
      "onClick={() => navigate({ kind: 'picker' })}",
    ],
  };
  const isComment = (l) => l.startsWith('//') || l.startsWith('*') || l.startsWith('/*');
  const writeViolations = [];
  for (const [rel, allow] of Object.entries(ROUTE_WRITE_ALLOWLIST)) {
    const src = read(join(webRoot, rel), 'utf8').split('\n');
    src.forEach((raw, i) => {
      const line = raw.trim();
      if (isComment(line)) return;
      // Only bare routes.ts calls are browser writes. `route.navigate(...)`
      // is the authorized command-side entry into the store/controller seam.
      if (!/(?:^|[^\w.])(navigate|replaceRoute)\(/.test(line)) return;
      if (allow.includes(line)) return;
      writeViolations.push(`${rel}:${i + 1}: ${line}`);
    });
  }
  if (writeViolations.length > 0) {
    console.error('check-substrate-boundaries: direct workspace-route write survives the cutover (must go through routeStore + the mounted controller):');
    for (const v of writeViolations) console.error('  ' + v);
    process.exit(1);
  }
}

// Presentation dispatch has single ownership. The catalog-derived decision
// belongs to actionPresentation and may cross into production only at the
// ActionPanel mount site. Tests owned by that module may import the resolver
// directly to pin the partition; no other caller may re-derive or consume it.
{
  const RESOLVER_REFERENCE_ALLOWLIST = new Set([
    'src/components/action-panel/actionPresentation.tsx',
    'src/components/ActionPanel.tsx',
    'tests/component/ActionPresentationFoundation.test.tsx',
    'tests/component/GenericPresentationClosure.test.tsx',
    'tests/component/TypedPluginActionUI.test.tsx',
    'tests/unit/actionPresentationCatalogFixture.test.ts',
    'tests/unit/savedActionSpecV1Reset.test.ts',
  ]);
  const resolverReferencePattern = /\b(?:deriveActionPresentationCatalog|resolveActionPresentation)\b/;
  const presentationViolations = [];
  for (const root of ['src', 'tests']) {
    for (const file of walk(join(webRoot, root))) {
      const rel = file.slice(webRoot.length + 1);
      if (RESOLVER_REFERENCE_ALLOWLIST.has(rel)) continue;
      if (resolverReferencePattern.test(readFileSync(file, 'utf8'))) {
        presentationViolations.push(rel);
      }
    }
  }
  if (presentationViolations.length > 0) {
    console.error('check-substrate-boundaries: presentation derivation import escapes its single owner (only ActionPanel.tsx and actionPresentation tests may import it):');
    for (const v of presentationViolations) console.error(`  ${v}`);
    process.exit(1);
  }
}

// WEB-04C's reset is a genuinely syntactic ownership/deletion rule, so Rule 19
// assigns it to this dedicated architecture script rather than an ordinary
// test that reads implementation source. Runtime tests own saved-envelope
// behavior; this AST check owns absence of the displaced reader/adapter class
// and forbids a per-kind table from appearing in the one generic codec.
{
  const adapterRel = 'src/components/action-panel/LegacyPresentationAdapter.tsx';
  const actionPanelRel = 'src/components/ActionPanel.tsx';
  const presentationRel = 'src/components/action-panel/actionPresentation.tsx';
  const savedSpecRel = 'src/actions/savedActionSpec.ts';
  const resetViolations = [];

  if (existsSync(join(webRoot, adapterRel))) {
    resetViolations.push(`${adapterRel} still exists`);
  }

  function parseTypeScript(rel) {
    const source = readFileSync(join(webRoot, rel), 'utf8');
    return ts.createSourceFile(
      rel,
      source,
      ts.ScriptTarget.ES2022,
      true,
      rel.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );
  }

  const actionPanelAst = parseTypeScript(actionPanelRel);
  let savedSpecImport = false;
  const visitActionPanel = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      if (node.moduleSpecifier.text.endsWith('LegacyPresentationAdapter')) {
        resetViolations.push(`${actionPanelRel} still imports LegacyPresentationAdapter`);
      }
      if (node.moduleSpecifier.text === '../actions/savedActionSpec') {
        const bindings = node.importClause?.namedBindings;
        savedSpecImport = Boolean(
          bindings
          && ts.isNamedImports(bindings)
          && bindings.elements.some((element) => element.name.text === 'decodeSavedActionSpec'),
        );
      }
    }
    const callableName = ts.isFunctionDeclaration(node)
      ? node.name?.text
      : ts.isVariableDeclaration(node)
        && ts.isIdentifier(node.name)
        && node.initializer
        && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
          ? node.name.text
          : undefined;
    if (
      callableName === 'normalizeSavedSpec'
      || /^saved[A-Z]/.test(callableName ?? '')
      || /SavedSpec/.test(callableName ?? '')
    ) {
      resetViolations.push(`${actionPanelRel} still declares reader ${callableName}`);
    }
    ts.forEachChild(node, visitActionPanel);
  };
  visitActionPanel(actionPanelAst);
  if (!savedSpecImport) {
    resetViolations.push(`${actionPanelRel} does not import decodeSavedActionSpec from the generic owner`);
  }

  const presentationAst = parseTypeScript(presentationRel);
  const visitPresentation = (node) => {
    if (ts.isStringLiteralLike(node) && node.text === 'frozen-legacy') {
      resetViolations.push(`${presentationRel} still declares frozen-legacy presentation`);
    }
    ts.forEachChild(node, visitPresentation);
  };
  visitPresentation(presentationAst);

  const savedSpecPath = join(webRoot, savedSpecRel);
  if (!existsSync(savedSpecPath)) {
    resetViolations.push(`${savedSpecRel} is missing`);
  } else {
    const savedSpecAst = parseTypeScript(savedSpecRel);
    const allowedImports = new Set(['ajv', '../api/types']);
    const visitSavedSpecImports = (node) => {
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
        const specifier = node.moduleSpecifier.text;
        if (!allowedImports.has(specifier) && !specifier.startsWith('ajv/')) {
          resetViolations.push(`${savedSpecRel} imports unauthorized dependency ${specifier}`);
        }
      }
      if (
        ts.isCallExpression(node)
        && node.expression.kind === ts.SyntaxKind.ImportKeyword
      ) {
        resetViolations.push(`${savedSpecRel} uses a dynamic import`);
      }
      ts.forEachChild(node, visitSavedSpecImports);
    };
    visitSavedSpecImports(savedSpecAst);
  }

  // Repo-wide ownership: the generic codec cannot hide a per-kind table in a
  // helper module, and no second saved/proposal translation owner may appear
  // under an innocuous directory. This is a syntactic architecture check, not
  // a test reading source under Rule 19.
  const savedTranslationCallable = /(?:saved|proposal).*(?:rehydrat|translat|normaliz)|(?:rehydrat|translat|normaliz).*(?:saved|proposal)/i;
  const savedTranslationRegistry = /(?:saved|proposal).*(?:rehydrat|translat|registry)|(?:rehydrat|translat|registry).*(?:saved|proposal)/i;
  const codecImporters = [];
  const decoderCallSites = [];
  for (const file of walk(join(webRoot, 'src'))) {
    const rel = file.slice(webRoot.length + 1);
    if (/\.(?:test|spec)\.[tj]sx?$/.test(rel)) continue;
    const ast = parseTypeScript(rel);
    if (savedTranslationRegistry.test(rel)) {
      resetViolations.push(`${rel} is a forbidden saved/proposal translation module`);
    }
    const visitProduction = (node) => {
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
        if (node.moduleSpecifier.text.endsWith('/savedActionSpec')) codecImporters.push(rel);
      }
      if (
        ts.isCallExpression(node)
        && ts.isIdentifier(node.expression)
        && node.expression.text === 'decodeSavedActionSpec'
      ) {
        decoderCallSites.push(rel);
      }
      const callableName = ts.isFunctionDeclaration(node)
        ? node.name?.text
        : ts.isMethodDeclaration(node) && ts.isIdentifier(node.name)
          ? node.name.text
          : ts.isVariableDeclaration(node)
            && ts.isIdentifier(node.name)
            && node.initializer
            && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
              ? node.name.text
              : undefined;
      if (
        rel !== actionPanelRel
        && callableName
        && savedTranslationCallable.test(callableName)
      ) {
        resetViolations.push(`${rel} declares forbidden translation callable ${callableName}`);
      }
      const declarationName = ts.isClassDeclaration(node)
        || ts.isInterfaceDeclaration(node)
        || ts.isTypeAliasDeclaration(node)
        || ts.isEnumDeclaration(node)
        ? node.name?.text
        : ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
          ? node.name.text
          : undefined;
      if (declarationName && savedTranslationRegistry.test(declarationName)) {
        resetViolations.push(`${rel} declares forbidden translation registry ${declarationName}`);
      }
      ts.forEachChild(node, visitProduction);
    };
    visitProduction(ast);
  }
  if (codecImporters.length !== 1 || codecImporters[0] !== actionPanelRel) {
    resetViolations.push(
      `generic codec must have one production importer (${actionPanelRel}); found ${codecImporters.join(', ') || 'none'}`,
    );
  }
  if (decoderCallSites.length !== 1 || decoderCallSites[0] !== actionPanelRel) {
    resetViolations.push(
      `decodeSavedActionSpec must have one production call (${actionPanelRel}); found ${decoderCallSites.join(', ') || 'none'}`,
    );
  }

  if (resetViolations.length > 0) {
    console.error('check-substrate-boundaries: saved-spec v1 reset is incomplete:');
    for (const violation of resetViolations) console.error(`  ${violation}`);
    process.exit(1);
  }
}

console.log('check-substrate-boundaries: OK (React-hook + route/presentation + generic saved-spec ownership)');
process.exit(0);
