/** Resolve the served catalog through the same templates and ribbon as the UI.
 * Bundle with esbuild, then pipe the normalized server catalog JSON to stdin. */
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import type { ActionCatalogPayload } from '../src/api/types';
import { actionTemplatesFromCatalog } from '../src/actions/model';
import { resolveRibbonTabs } from '../src/workbench/actSurface';

export function docsCatalog(catalog: ActionCatalogPayload) {
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
      return [{ id: item.launcherKind, label: item.label, description: item.description,
        path: [tab.label, group.caption], params: template.params ?? [],
        inputSchema: entry.input_schema, outputSchema: entry.output_schema }];
    })));
  return { version: 1, actions };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const catalog = JSON.parse(readFileSync(0, 'utf8')) as ActionCatalogPayload;
  process.stdout.write(JSON.stringify(docsCatalog(catalog), null, 2) + '\n');
}
