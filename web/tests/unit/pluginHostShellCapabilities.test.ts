import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import ts from 'typescript';

type HostFamily = 'panel' | 'view' | 'projectionView';

const REPO_ROOT = fileURLToPath(new URL('../../..', import.meta.url));

const GOLDEN_CAPABILITIES = {
  panel: [
    'sheet.active',
    'selection.rows',
    'host.navigation.openRow',
    'grid.state.read',
    'action.run',
  ],
  view: [
    'sheet.rows.read',
    'media.blob.resolve',
    'host.navigation.openRow',
    'grid.state.read',
    'grid.filter.applyBbox',
    'action.run',
    'host.library.deckgl',
  ],
  projectionView: [
    'projection.status',
    'projection.build',
    'projection.artifact.read',
    'projection.data.read',
    'host.navigation.openRow',
    'grid.state.read',
    'grid.filter.applyBbox',
    'action.run',
    'host.library.deckgl',
  ],
} satisfies Record<HostFamily, string[]>;

const GOLDEN_DECLARATION_BYTES = {
  panel: `export const DEFAULT_PLUGIN_PANEL_CAPABILITIES: PluginPanelCapability[] = [
  'sheet.active',
  'selection.rows',
  'host.navigation.openRow',
  'grid.state.read',
  'action.run',
];`,
  view: `const DEFAULT_PLUGIN_VIEW_CAPABILITIES: PluginViewCapability[] = [
  'sheet.rows.read',
  'media.blob.resolve',
  'host.navigation.openRow',
  'grid.state.read',
  'grid.filter.applyBbox',
  'action.run',
  'host.library.deckgl',
];`,
  projectionView: `export const DEFAULT_PLUGIN_PROJECTION_VIEW_CAPABILITIES: PluginProjectionViewCapability[] = [
  'projection.status',
  'projection.build',
  'projection.artifact.read',
  'projection.data.read',
  'host.navigation.openRow',
  'grid.state.read',
  // Projection views wire the same grid-filter fragment plain views get —
  // the geo map's "filter to this area" applies the canonical
  // {geo_col: {bbox}} filter.
  'grid.filter.applyBbox',
  'action.run',
  'host.library.deckgl',
];`,
} satisfies Record<HostFamily, string>;

const FAMILY_SOURCES = {
  panel: {
    path: 'web/src/workbench/pluginPanelContext.ts',
    constant: 'DEFAULT_PLUGIN_PANEL_CAPABILITIES',
  },
  view: {
    path: 'web/src/workbench/pluginViewContext.ts',
    constant: 'DEFAULT_PLUGIN_VIEW_CAPABILITIES',
  },
  projectionView: {
    path: 'web/src/workbench/pluginProjectionViewContext.ts',
    constant: 'DEFAULT_PLUGIN_PROJECTION_VIEW_CAPABILITIES',
  },
} satisfies Record<HostFamily, { path: string; constant: string }>;

interface CapabilityDeclaration {
  initializer: ts.ArrayLiteralExpression;
  sourceFile: ts.SourceFile;
  statement: ts.VariableStatement;
  values: string[];
}

function readRepoFile(path: string): string {
  return readFileSync(new URL(path, `file://${REPO_ROOT}/`), 'utf8');
}

function capabilityDeclaration(family: HostFamily): CapabilityDeclaration {
  const { path, constant } = FAMILY_SOURCES[family];
  const sourceText = readRepoFile(path);
  const sourceFile = ts.createSourceFile(path, sourceText, ts.ScriptTarget.Latest, true);
  let declaration: ts.VariableDeclaration | undefined;

  function visit(node: ts.Node): void {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.name.text === constant) {
      declaration = node;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);

  expect(declaration, `${constant} must remain declared`).toBeDefined();
  const initializer = declaration?.initializer;
  expect(initializer && ts.isArrayLiteralExpression(initializer), `${constant} must own an array literal`).toBe(
    true,
  );
  if (!initializer || !ts.isArrayLiteralExpression(initializer)) {
    throw new Error(`${constant} must own an array literal`);
  }
  expect(initializer.elements.every(ts.isStringLiteral), `${constant} may not spread or alias another family`).toBe(
    true,
  );
  const statement = declaration?.parent.parent;
  if (!statement || !ts.isVariableStatement(statement)) {
    throw new Error(`${constant} must remain a standalone variable statement`);
  }
  return {
    initializer,
    sourceFile,
    statement,
    values: initializer.elements.map((element) => {
      if (!ts.isStringLiteral(element)) throw new Error(`${constant} entries must be string literals`);
      return element.text;
    }),
  };
}

function sdkAvailableCapabilities(family: HostFamily): string[] {
  const manifest = JSON.parse(readRepoFile('sdk/contract-manifest.json')) as {
    capabilityDefaults: Record<HostFamily, string[]>;
    capabilityDeclaredOnly: Partial<Record<HostFamily, string[]>>;
  };
  return [
    ...manifest.capabilityDefaults[family],
    ...(manifest.capabilityDeclaredOnly[family] ?? []),
  ];
}

describe('plugin host capability authority fixture', () => {
  it('freezes every family capability set and order at its pre-migration bytes', () => {
    for (const family of Object.keys(FAMILY_SOURCES) as HostFamily[]) {
      const declaration = capabilityDeclaration(family);
      expect(declaration.statement.getText(declaration.sourceFile)).toBe(
        GOLDEN_DECLARATION_BYTES[family],
      );
      expect(declaration.values).toEqual(GOLDEN_CAPABILITIES[family]);
      expect(new Set(declaration.values)).toEqual(new Set(GOLDEN_CAPABILITIES[family]));
      expect(sdkAvailableCapabilities(family)).toEqual(GOLDEN_CAPABILITIES[family]);
    }

  });

  it('keeps every per-family constant as a separate direct array declaration', () => {
    const declarations = (Object.keys(FAMILY_SOURCES) as HostFamily[]).map((family) =>
      capabilityDeclaration(family),
    );
    expect(new Set(declarations.map(({ sourceFile }) => sourceFile.fileName)).size).toBe(3);
    expect(declarations.every(({ initializer }) => initializer.elements.every(ts.isStringLiteral))).toBe(true);

    const shellSource = readRepoFile('web/src/workbench/pluginHostShell.tsx');
    for (const { constant } of Object.values(FAMILY_SOURCES)) {
      expect(shellSource).not.toContain(constant);
    }

    const shellSourceFile = ts.createSourceFile(
      'pluginHostShell.tsx',
      shellSource,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TSX,
    );
    const shellProps = shellSourceFile.statements.find(
      (statement): statement is ts.InterfaceDeclaration =>
        ts.isInterfaceDeclaration(statement) && statement.name.text === 'PluginHostShellProps',
    );
    expect(shellProps, 'the shell props contract must remain explicit').toBeDefined();
    expect(
      shellProps?.members.map((member) =>
        member.name && ts.isIdentifier(member.name) ? member.name.text : undefined,
      ),
    ).toEqual(['descriptor', 'sheet', 'resolveAvailability', 'renderUnavailable', 'mount']);
  });
});
