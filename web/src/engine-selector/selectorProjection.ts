import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse, SelectorChoice } from '../api/selectorChoices';
import type { SelectorChoicesState } from './useSelectorChoices';

/** Readiness never borrows a default when an explicit orphaned choice exists. */
export function currentSelectorChoice(state: Pick<SelectorChoicesState, 'response' | 'stale' | 'error'>): SelectorChoice | null {
  if (state.stale || state.error || !state.response) return null;
  const response = state.response;
  const id = response.current_choice_id ?? response.default_choice_id;
  if (!id) return null;
  return response.orphaned_current?.choice_id === id ? response.orphaned_current
    : response.groups.flatMap((group) => group.choices).find((choice) => choice.choice_id === id) ?? null;
}

export function selectorQueryKey(query: HttpSelectorChoicesQuery, response: HttpSelectorChoicesResponse | null): string {
  const subject = query.subject;
  if (subject.kind === 'action') {
    const names = new Set([subject.field, 'engine', 'model', ...(response?.depends_on ?? [])]);
    const params = Object.fromEntries(Object.entries(subject.params ?? {}).filter(([name]) => names.has(name)));
    return JSON.stringify({ kind: subject.kind, action_id: subject.action_id, field: subject.field, params });
  }
  return JSON.stringify(subject);
}
