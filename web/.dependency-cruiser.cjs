/**
 * Architectural import boundaries for web/src (dependency-cruiser).
 *
 * Boundary rules are structural checks, not string-presence tests. They live in
 * a dependency-cruiser config executed inside the web lint singleton.
 * Run them with `npm --prefix web run lint`. A violating *import* — not a missing string —
 * is what turns these red.
 *
 * Rules encode intent:
 *  - UI islands reach the backend only through the composed api/ports/contracts
 *    seam, never the implementation module (api/real.ts).
 *  - Application modules outside api/ and generated/ consume generated HTTP
 *    contracts through the api/ seam.
 *  - src/generated is generated output: import-only, it depends on nothing in
 *    the app tree.
 *  - unsupported module paths such as RecipePanel and api/mock cannot be
 *    imported.
 */
module.exports = {
  forbidden: [
    {
      name: 'open-editions-no-private-ui',
      comment:
        'Public composition modules and local/team entrypoints accept private ' +
        'contributions as data and never import the private frontend tree.',
      severity: 'error',
      from: {
        path: '^src/(editions/posture|settings/openSettingsRegistry|routes/openRoutes|api/open|api/openTransport|entries/(local|team))\\.(ts|tsx)$',
      },
      to: {
        path: '^src/private/',
      },
    },
    {
      name: 'ui-islands-no-raw-transport',
      comment:
        'UI islands (components/workbench/grid/media/embeddings/settings) must ' +
        'not import the implementation module web/src/api/real.ts. Go through the ' +
        'public composition surface (web/src/api/open.ts), the port interfaces ' +
        '(web/src/api/ports.ts), or the domain contract adapters.',
      severity: 'error',
      from: {
        path: '^src/(components|workbench|grid|media|embeddings|settings)/',
      },
      to: {
        path: '^src/api/real\\.ts$',
      },
    },
    {
      name: 'application-no-generated-http-contracts',
      comment:
        'Application modules outside api/ and generated/ consume API-facing ' +
        'contracts through the api/ seam.',
      severity: 'error',
      from: {
        path: '^src/',
        pathNot: '^src/(api|generated)/',
      },
      to: {
        path: '^src/generated/openHttpContracts\\.ts$',
      },
    },
    {
      name: 'generated-is-import-only',
      comment:
        'web/src/generated is generated output (openHttpContracts.ts): it is imported ' +
        'by the app but must not import app modules itself. Regenerate the source ' +
        'contract instead of hand-wiring app dependencies into generated code.',
      severity: 'error',
      from: {
        path: '^src/generated/',
      },
      to: {
        path: '^src/',
        pathNot: '^src/generated/',
      },
    },
    {
      name: 'no-deleted-legacy-modules',
      comment:
        'These paths are not application modules. Components use ActionPanel, ' +
        'and frontend API composition uses the supported api/open.ts surface.',
      severity: 'error',
      from: {},
      to: {
        path: 'src/components/RecipePanel|src/api/mock\\.ts',
      },
    },
  ],
  options: {
    doNotFollow: {
      path: 'node_modules',
    },
    includeOnly: '^src/',
    tsPreCompilationDeps: true,
    tsConfig: {
      fileName: 'tsconfig.app.json',
    },
    enhancedResolveOptions: {
      exportsFields: ['exports'],
      conditionNames: ['import', 'require', 'node', 'default', 'types'],
      extensions: ['.js', '.jsx', '.ts', '.tsx', '.d.ts'],
    },
  },
};
