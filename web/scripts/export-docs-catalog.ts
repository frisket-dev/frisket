/** Resolve the served catalog through the same templates and ribbon as the UI.
 * Bundle with esbuild, then pipe the normalized server catalog JSON to stdin. */
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import type { ActionCatalogPayload } from '../src/api/types';
import { actionTemplatesFromCatalog } from '../src/actions/model';
import { customFormParams } from '../src/actions/formLabels';
import { resolveRibbonTabs } from '../src/workbench/actSurface';

export function docsCatalog(catalog: ActionCatalogPayload, appRevision: string) {
  if (!/^[0-9a-f]{40}$/.test(appRevision)) throw new Error('App revision must be a full lowercase Git SHA');
  const templates = actionTemplatesFromCatalog(catalog);
  const byKind = new Map(templates.map((template) => [template.kind, template]));
  const context = { activeProject: false, activeSheet: false, activeRow: false, activeColumn: false,
    activeCell: false, activeEvidence: false, activeSource: false, activeEntity: false,
    activeProjection: false, selectedRowIds: [], columnTypes: [] };
  const actions = resolveRibbonTabs(templates, context).filter((tab) => !tab.contextual)
    .flatMap((tab) => tab.groups.flatMap((group) => group.items.flatMap((item) => {
      if (item.kind !== 'action') return [];
      const template = byKind.get(item.launcherKind)!;
      const entry = catalog.actions.find((candidate) => candidate.kind === item.launcherKind)!;
      const auditedParams = customFormParams(item.launcherKind, template.params ?? []);
      return [{ id: item.launcherKind, label: item.label, description: item.description,
        path: [tab.label, group.caption], params: auditedParams ?? [],
        settingsCoverage: auditedParams ? 'audited' : 'not-audited',
        inputSchema: entry.input_schema, outputSchema: entry.output_schema }];
    })));
  return { version: 1, appRevision, actions };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const head = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  try {
    execFileSync('git', ['diff', '--quiet', 'HEAD', '--', 'src', 'web/src',
      'web/scripts/export-docs-catalog.ts', 'scripts/ci/gen_action_presentation_catalog_fixture.py']);
  } catch {
    throw new Error('Catalog source files differ from checkout HEAD');
  }
  const args = process.argv.slice(2);
  if (args.length !== 0 && (args.length !== 2 || args[0] !== '--revision')) {
    throw new Error('Usage: export-catalog.sh [--revision <full HEAD SHA>]');
  }
  const revision = args[1] ?? head;
  if (!/^[0-9a-f]{40}$/.test(revision) || revision !== head) {
    throw new Error('Catalog revision must equal the current checkout HEAD');
  }
  const catalog = JSON.parse(readFileSync(0, 'utf8')) as ActionCatalogPayload;
  process.stdout.write(JSON.stringify(docsCatalog(catalog, revision), null, 2) + '\n');
}
