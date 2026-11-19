import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse } from '../../src/generated/openHttpContracts';

export function mediaChoices(query: HttpSelectorChoicesQuery, engines: string[], canRun = true): HttpSelectorChoicesResponse {
  if (query.subject.kind !== 'action') throw new Error('mediaChoices answers action selectors only');
  const { action_id, field, params } = query.subject;
  const current = params?.engine;
  return {
    schema_version: 'frisket.selector_choices.v3', project_id: 'test-project',
    subject: { kind: 'action', action_id, field },
    depends_on: ['language', 'model_size', 'vad', 'diarize', 'detail'],
    current_choice_id: typeof current === 'string' && engines.includes(current) ? current : null,
    default_choice_id: engines[0] ?? null, orphaned_current: null, cloud_setup: null,
    groups: [{ group_id: 'local', kind: 'local', label: 'On this device', status: canRun ? 'ready' : 'needs_setup', choices: engines.map((id) => ({
      choice_id: id, label: id, summary: '', description: '', facts: [], model_card_url: null,
      authored_selection: { kind: 'engine', engine: id }, resolved_target: null,
      processing_destination: { kind: 'local', label: 'On this device' },
      execution_route: null, route_selection: null,
      status: canRun ? 'ready' : 'needs_setup', can_author: true, can_run: canRun,
      blocker: canRun ? null : { code: 'provider_key_required', message: 'Setup required', field: null },
      setup: null, configuration_actions: [], active_operation: null,
      is_current: current === id, is_default: false,
    })) }],
  };
}
