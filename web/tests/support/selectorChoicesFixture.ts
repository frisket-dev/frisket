import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { vi } from 'vitest';

import { listProviders } from '../../src/api/open';
import { selectorChoicesApi, type SelectorChoice } from '../../src/api/selectorChoices';
import type { GeneratedActionCatalogEntry, LocalProviderCatalog } from '../../src/api/types';
import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse } from '../../src/generated/openHttpContracts';
import { installDialogPolyfill } from './domPolyfills';

type Selection = Extract<SelectorChoice['authored_selection'], { kind: 'engine' | 'model' }>;

/** An explicitly admitted synthetic provider roster for form-only tests. */
export function selectorProviderCatalog(modelIds: string[]): LocalProviderCatalog {
  return {
    schemaVersion: 'frisket.providers.v1', tier: 'local',
    providers: [{ id: 'test', label: 'Test provider', kind: 'platform_api', configured: true,
      source: 'env', hint: null, models: modelIds.map((id) => ({ id, label: id, price: null })) }],
  };
}

function choice(selection: Selection, label: string, available: boolean, reason?: string): SelectorChoice {
  return {
    choice_id: JSON.stringify(selection), authored_selection: selection, label,
    summary: selection.kind === 'model' ? selection.model : selection.model ?? selection.engine,
    description: '', facts: [], resolved_target: null,
    processing_destination: { kind: 'unknown', label: 'Test execution target' },
    status: available ? 'ready' : 'unavailable', can_author: available, can_run: available,
    blocker: available ? null : { code: 'fixture_unavailable', message: reason ?? 'Unavailable in this catalog', field: null },
    setup: null, active_operation: null, model_card_url: null, is_current: false, is_default: false,
  };
}

/** Project this test's explicit action/provider rosters at the typed HTTP API
 * boundary. The real selector, authored selection, and execution gate remain mounted. */
export function installActionSelectorFixture(
  entry: GeneratedActionCatalogEntry,
  providers: () => Promise<LocalProviderCatalog> = () => listProviders(),
) {
  installDialogPolyfill();
  return vi.spyOn(selectorChoicesApi, 'getSelectorChoices').mockImplementation(async (projectId, query) => {
    if (query.subject.kind !== 'action' || query.subject.action_id !== entry.kind) {
      throw new Error(`Unexpected selector subject for ${entry.kind}`);
    }
    let catalog: LocalProviderCatalog;
    try { catalog = await providers(); } catch { catalog = { schemaVersion: 'frisket.providers.v1', tier: 'local', providers: [] }; }
    return actionSelectorResponse(projectId, query, entry, catalog);
  });
}

export function actionSelectorResponse(
  projectId: string, query: HttpSelectorChoicesQuery, entry: GeneratedActionCatalogEntry,
  catalog: LocalProviderCatalog,
): HttpSelectorChoicesResponse {
  if (query.subject.kind !== 'action') throw new Error('An action selector subject is required');
  const subject = query.subject;
  const mixed = entry.ui_hints.semantic_controls.engine === 'engine';
  const engines = entry.ui_hints.engines ?? [];
  const llm = engines.find((engine) => engine.id === 'llm');
  const groups: HttpSelectorChoicesResponse['groups'] = [];
  const fixed = engines.filter((engine) => engine.id !== 'llm').map((engine) => choice(
    { kind: 'engine', engine: engine.id, ...(mixed ? { model: null } : {}) },
    engine.label, engine.available, engine.error,
  ));
  if (fixed.length) groups.push({ group_id: 'local', kind: 'local', label: 'Local', status: 'ready', choices: fixed });
  for (const provider of catalog.providers) {
    const models = provider.models.map((model) => choice(
      mixed ? { kind: 'engine', engine: 'llm', model: model.id } : { kind: 'model', model: model.id },
      model.label, provider.configured && (!mixed || llm?.available === true),
      mixed && llm?.available !== true ? llm?.error ?? 'The LLM engine is not offered' : 'Provider setup is required',
    ));
    if (models.length) groups.push({ group_id: provider.id, kind: 'provider', label: provider.label,
      status: models.some((model) => model.can_run) ? 'ready' : 'unavailable', choices: models });
  }
  const choices = groups.flatMap((group) => group.choices);
  const params = subject.params ?? {};
  const engine = typeof params.engine === 'string' ? params.engine : undefined;
  const model = typeof params.model === 'string' ? params.model : undefined;
  const current: Selection | null = mixed
    ? engine && (engine !== 'llm' || model) ? { kind: 'engine', engine, model: model ?? null } : null
    : model ? { kind: 'model', model } : null;
  const key = current ? JSON.stringify(current) : null;
  const selected = choices.find((candidate) => candidate.choice_id === key);
  const defaultEngine = entry.input_schema.properties?.engine?.default;
  const preferred = choices.find((candidate) => candidate.can_run && candidate.authored_selection.kind === 'engine'
    && candidate.authored_selection.engine === defaultEngine) ?? choices.find((candidate) => candidate.can_run);
  if (selected) selected.is_current = true;
  if (preferred) preferred.is_default = true;
  const orphan = current && !selected ? choice(current, engine ?? model ?? 'Saved choice', false, 'Saved choice is not offered') : null;
  if (orphan) orphan.is_current = true;
  return { schema_version: 'frisket.selector_choices.v1', project_id: projectId,
    subject: { kind: 'action', action_id: entry.kind, field: subject.field },
    depends_on: mixed ? ['engine', 'model'] : ['model'], current_choice_id: selected?.choice_id ?? orphan?.choice_id ?? null,
    default_choice_id: preferred?.choice_id ?? null, groups, orphaned_current: orphan };
}

export function selectorTrigger() {
  const field = screen.queryByTestId('field-engine') ?? screen.getByTestId('field-model');
  return within(field).getByRole('button');
}

export async function openActionSelector() {
  await waitFor(() => { if (selectorTrigger().hasAttribute('disabled')) throw new Error('Selector is loading'); });
  fireEvent.click(selectorTrigger());
  return screen.findByTestId('engine-selector-dialog');
}

export async function chooseActionSelector(value: string) {
  const dialog = await openActionSelector();
  fireEvent.change(within(dialog).getByRole('searchbox', { name: /^Search / }), { target: { value } });
  const button = Array.from(dialog.querySelectorAll<HTMLButtonElement>('[data-engine-selector-choice]'))
    .find((candidate) => candidate.textContent?.includes(value));
  if (!button) throw new Error(`No selector choice matching ${value}`);
  fireEvent.click(button);
}
