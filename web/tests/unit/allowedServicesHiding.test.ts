import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog, offeredActionTemplates } from '../../src/actions/model';
import type { HttpSelectorChoicesResponse, SelectorChoice } from '../../src/api/selectorChoices';
import { getProjectActionCatalog } from '../../src/api/httpContractRoutes';
import { withoutRefusedServices } from '../../src/engine-selector/selectorProjection';
import { resolveRibbonTabs } from '../../src/workbench/actSurface';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { mediaChoices } from '../support/mediaSelectorFixtures';
import { jsonResponse } from '../support/httpContractTestHelpers';
import { servedActionCatalog } from '../support/servedActionCatalog';

beforeAll(() => { servedActionCatalog(); }, 30_000);

afterEach(() => {
  vi.unstubAllGlobals();
});

function choice(id: string, overrides: Partial<SelectorChoice> = {}): SelectorChoice {
  const [base] = SELECTOR.groups[0].choices;
  return { ...base, choice_id: id, label: id, ...overrides };
}

const refusedBlocker = { code: 'service_not_allowed', message: "This project's Allowed services setting is This machine only; it does not allow this service.", field: null };
const keyBlocker = { code: 'provider_key_required', message: 'Add an OpenAI key.', field: null };

const SELECTOR = mediaChoices({
  schema_version: 'frisket.selector_choices_query.v1',
  subject: { kind: 'action', action_id: 'media.transcribe', field: 'engine', params: {} },
}, ['local']);

describe('pickers under a restricted Allowed services setting', () => {
  it('hides refused choices and routes and keeps other unavailability', () => {
    const response: HttpSelectorChoicesResponse = {
      ...SELECTOR,
      groups: [
        {
          ...SELECTOR.groups[0],
          choices: [
            choice('local'),
            choice('remote', { status: 'unavailable', blocker: refusedBlocker }),
            choice('needs-key', { status: 'unavailable', blocker: keyBlocker }),
            choice('routed', {
              route_selection: {
                can_mutate: true,
                model_id: 'routed',
                selected: 'own',
                options: [
                  { id: 'own', label: 'Your server', status: 'ready' },
                  { id: 'cloud', label: 'Frisket Cloud', status: 'unavailable', disabled_reason: refusedBlocker.message, disabled_code: 'service_not_allowed' },
                ],
              },
            }),
          ],
        },
        { ...SELECTOR.groups[0], choices: [choice('remote-only', { status: 'unavailable', blocker: refusedBlocker })] },
      ],
      orphaned_current: choice('saved-remote', { status: 'unavailable', blocker: refusedBlocker }),
    };

    const shown = withoutRefusedServices(response);

    expect(shown.groups).toHaveLength(1);
    expect(shown.groups[0].choices.map((entry) => entry.choice_id)).toEqual(['local', 'needs-key', 'routed']);
    expect(shown.groups[0].choices[1].blocker).toEqual(keyBlocker);
    expect(shown.groups[0].choices[2].route_selection?.options.map((option) => option.id)).toEqual(['own']);
    expect(shown.orphaned_current).toBeNull();
  });

  it('does not offer an action with no allowed service in the ribbon, menus or palette', () => {
    const catalog = servedActionCatalog();
    const refusedKind = 'map.translate';
    const restricted = {
      ...catalog,
      actions: catalog.actions.map((entry) => (entry.kind === refusedKind
        ? { ...entry, ui_hints: { ...entry.ui_hints, network_disabled: true, service_not_allowed: true } }
        : entry)),
    };
    const resolved = actionTemplatesFromCatalog(restricted);
    expect(resolved.some((template) => template.kind === refusedKind)).toBe(true);
    const templates = offeredActionTemplates(resolved, restricted);
    expect(templates.some((template) => template.kind === refusedKind)).toBe(false);
    expect(templates.some((template) => template.kind === 'map.classify')).toBe(true);
    const ribbonKinds = (shown: typeof templates) => resolveRibbonTabs(shown, {
      activeProject: true, activeSheet: true, activeRow: false, activeColumn: false,
      activeCell: false, activeEvidence: false, activeFeed: false, activeEntity: false,
      activeProjection: false, selectedRowIds: [], columnTypes: [],
    }).flatMap((tab) => tab.groups.flatMap((group) => group.items.flatMap((item) => (
      item.kind === 'action' ? [item.launcherKind] : []
    ))));
    expect(ribbonKinds(actionTemplatesFromCatalog(catalog))).toContain(refusedKind);
    expect(ribbonKinds(templates)).not.toContain(refusedKind);
  });

  it('drops catalog engines Allowed services refuses and keeps other unavailable engines', async () => {
    const entry = syntheticActionCatalogEntry('map.translate', {
      ui_hints: {
        engines: [
          { id: 'opus_mt', label: 'Opus MT', tier: 'local', available: true },
          { id: 'deepl', label: 'DeepL', tier: 'remote', available: false, error: refusedBlocker.message, error_code: 'service_not_allowed' },
          { id: 'google', label: 'Google', tier: 'remote', available: false, error: 'Add a Google key.' },
        ],
      },
    });
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.action_catalog.v2',
      action_schema: {},
      error_schema: {},
      result_schema: {},
      receipt_schema: {},
      validation_result_schema: {},
      actions: [entry],
    })));

    const catalog = await getProjectActionCatalog('project-1');

    expect(catalog.actions[0].ui_hints.engines?.map((engine) => engine.id)).toEqual(['opus_mt', 'google']);
  });
});
