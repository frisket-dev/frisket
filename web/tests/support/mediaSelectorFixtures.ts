import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse } from '../../src/generated/openHttpContracts';

export function mediaChoices(query: HttpSelectorChoicesQuery, engines: string[], canRun = true): HttpSelectorChoicesResponse {
  const current = query.subject.kind === 'action' ? query.subject.params?.engine : null;
  return {
    schema_version: 'frisket.selector_choices.v1', project_id: 'test-project', subject: query.subject,
    depends_on: ['language', 'model_size', 'vad', 'diarize', 'detail'],
    current_choice_id: typeof current === 'string' && engines.includes(current) ? current : null,
    default_choice_id: engines[0] ?? null, orphaned_current: null,
    groups: [{ group_id: 'local', label: 'On this device', choices: engines.map((id) => ({
      choice_id: id, label: id, summary: '', description: '', facts: [], model_card_url: null,
      authored_selection: { kind: 'engine', engine: id }, resolved_target: null,
      processing_destination: { kind: 'local', label: 'On this device' },
      status: canRun ? 'ready' : 'needs_setup', can_author: true, can_run: canRun,
      blocker: canRun ? null : { code: 'provider_key_required', message: 'Setup required', field: null },
      setup: null, active_operation: null, is_current: current === id, is_default: false,
    })) }],
  } as HttpSelectorChoicesResponse;
}
