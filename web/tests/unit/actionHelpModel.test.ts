import { beforeAll, describe, expect, it } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import {
  BUILTIN_ACTION_HELP,
  buildActionHelp,
} from '../../src/components/action-panel/actionHelpModel';
import {
  completeMappedActionCatalog,
  syntheticActionCatalogEntry,
} from '../support/actionCatalogFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';

beforeAll(() => { servedActionCatalog(); }, 30_000);

describe('action help content', () => {
  it('keeps every authored help entry attached to a canonical served action', () => {
    const servedKinds = new Set(servedActionCatalog().actions.map((entry) => entry.kind));
    for (const [kind, content] of Object.entries(BUILTIN_ACTION_HELP)) {
      expect(servedKinds.has(kind), kind).toBe(true);
      expect(content.how, `${kind} how`).toMatch(/\.$/);
      expect(content.goodFor, `${kind} goodFor`).toMatch(/\.$/);
      expect(content.how.length, `${kind} how`).toBeGreaterThan(35);
      expect(content.goodFor.length, `${kind} goodFor`).toBeGreaterThan(25);
    }
  });

  it('builds complete structured help for every action in the served catalog', () => {
    const catalog = completeMappedActionCatalog({
      stockEntries: [syntheticActionCatalogEntry('map.regex_extract', {
        ui_hints: { form: 'generated', semantic_controls: {}, logical_outputs: [] },
      })],
    });
    const actions = actionTemplatesFromCatalog(catalog);

    for (const action of actions) {
      const help = buildActionHelp(action);
      expect(help.summary, `${action.kind} summary`).toBeTruthy();
      const authored = BUILTIN_ACTION_HELP[action.kind];
      if (authored) expect(help.how, `${action.kind} how`).toBe(authored.how);
      else expect(help.how, `${action.kind} fallback how`).toBeTruthy();
      expect(Array.isArray(help.inputs), `${action.kind} inputs`).toBe(true);
      expect(help.output, `${action.kind} output`).toBeTruthy();
      expect(help.cost, `${action.kind} cost`).toBeTruthy();
      expect(help.goodFor, `${action.kind} goodFor`).toBeTruthy();
    }
  });

  it('describes model cost, local work, and new-sheet output honestly', () => {
    const catalog = completeMappedActionCatalog({
      stockEntries: [syntheticActionCatalogEntry('map.regex_extract'), syntheticActionCatalogEntry('derive.join')],
    });
    const actions = actionTemplatesFromCatalog(catalog);
    const byKind = (kind: string) => actions.find((action) => action.kind === kind)!;

    expect(buildActionHelp({ ...byKind('map.summarize'), llm: true }).cost).toMatch(/selected model/i);
    expect(buildActionHelp({
      kind: 'map.regex_extract',
      name: 'Regex extract',
      description: 'Extract matching text.',
      defaultPrompt: '',
      defaultFields: [],
    }).cost).toMatch(/without a language-model charge/i);
    expect(buildActionHelp(byKind('derive.join')).output).toMatch(/creates a new sheet/i);
  });
});
