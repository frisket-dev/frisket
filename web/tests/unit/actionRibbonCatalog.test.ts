import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { ActionCatalogEntry } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { resolveRibbonTabs } from '../../src/workbench/actSurface';
import { servedActionCatalog } from '../support/servedActionCatalog';

const catalog = servedActionCatalog();
const bundledPlugin = JSON.parse(readFileSync(
  '../src/frisket/authoring/bundled_plugins/frisket.transliterate/plugin.json', 'utf8',
)) as { runtime: { actions: { catalog_entry: ActionCatalogEntry }[] } };
const templates = actionTemplatesFromCatalog({ ...catalog,
  actions: [...catalog.actions, ...bundledPlugin.runtime.actions.map((action) => action.catalog_entry)],
});
const context = {
  activeProject: true, activeSheet: true, activeRow: false, activeColumn: false,
  activeCell: false, activeEvidence: false, activeSource: false, activeEntity: false,
  activeProjection: false, selectedRowIds: [], columnTypes: [],
};
const tabs = resolveRibbonTabs(templates, context);
const placements = tabs.flatMap((tab) => tab.groups.flatMap((group) => group.items.flatMap((item) => (
  item.kind === 'action' ? [{ kind: item.launcherKind, tab: tab.id, group: group.caption }] : []
))));

describe('served action ribbon organization', () => {
  it('keeps import and result workflows available without exposing their helper actions in navigation', () => {
    const helpers = templates.filter((template) => template.kind.startsWith('import.')
      || template.kind.startsWith('export.') || template.kind.startsWith('embedding.')
      || template.kind === 'resolve.entities');
    // These remain valid action templates: the ribbon policy must not remove
    // their drawers, saved drafts, or dedicated workflow entry points.
    expect(helpers.map((template) => template.kind)).toEqual(expect.arrayContaining([
      'import.xlsx', 'import.csv', 'import.append_xlsx', 'embedding.index_cluster', 'resolve.entities',
    ]));
    const visible = new Set(placements.map((placement) => placement.kind));
    expect(helpers.filter((template) => visible.has(template.kind))).toEqual([]);
    expect(tabs.flatMap((tab) => tab.groups.flatMap((group) => group.items)))
      .toEqual(expect.arrayContaining([
        expect.objectContaining({ kind: 'command', command: 'import' }),
      ]));
  });

  it('keeps six broad tabs with focused groups of at most five choices', () => {
    expect(tabs.map((tab) => tab.label)).toEqual([
      'Data', 'Media', 'Analyze', 'Transform', 'Tables', 'Research',
    ]);
    const items = tabs.flatMap((tab) => {
      for (const group of tab.groups) {
        expect(group.items.length, `${tab.label}/${group.caption}`).toBeLessThanOrEqual(5);
      }
      return tab.groups.flatMap((group) => group.items);
    });
    // All 49 standalone actions (including Transliterate) and 9 workflow or
    // compare commands remain present, with no duplicate base-tab launchers.
    expect(items).toHaveLength(58);
    expect(new Set(items.map((item) => item.kind === 'action' ? item.launcherKind
      : item.kind === 'command' ? item.command : item.contributionId)).size).toBe(58);
  });

  it('places standalone actions with related work rather than beside Template', () => {
    expect(placements).toEqual(expect.arrayContaining([
      { kind: 'map.regex_extract', tab: 'analyze', group: 'EXTRACT' },
      { kind: 'map.columns_from_json', tab: 'tables', group: 'TABLES' },
      { kind: 'derive.link_table', tab: 'tables', group: 'TABLES' },
      { kind: 'media.enclosure_materialize', tab: 'data', group: 'WEB' },
    ]));
    expect(placements.filter(({ tab, group }) => tab === 'transform' && group === 'TRANSFORM')
      .map(({ kind }) => kind)).toEqual(['map.clean_column', 'map.clean_dates', 'map.template', 'map.python']);
    expect(tabs.some((tab) => tab.id === 'misc')).toBe(false);
  });
});
