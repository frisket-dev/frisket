import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse, SelectorChoice } from '../api/selectorChoices';
import type { SelectorChoicesState } from './useSelectorChoices';
import { SERVICE_NOT_ALLOWED } from '../allowedServices';

function refused(choice: SelectorChoice): boolean {
  return choice.blocker?.code === SERVICE_NOT_ALLOWED;
}

function withAllowedRoutes(choice: SelectorChoice): SelectorChoice {
  const routes = choice.route_selection;
  if (!routes?.options.some((option) => option.disabled_code === SERVICE_NOT_ALLOWED)) return choice;
  return {
    ...choice,
    route_selection: {
      ...routes,
      options: routes.options.filter((option) => option.disabled_code !== SERVICE_NOT_ALLOWED),
    },
  };
}

/** Pickers hide what the project's Allowed services setting refuses; every
 *  other kind of unavailability keeps its own presentation. A group left
 *  with nothing to offer is dropped. */
export function withoutRefusedServices(response: HttpSelectorChoicesResponse): HttpSelectorChoicesResponse {
  return {
    ...response,
    groups: response.groups.flatMap((group) => {
      const choices = group.choices.filter((choice) => !refused(choice)).map(withAllowedRoutes);
      return choices.length === 0 && group.choices.length > 0 ? [] : [{ ...group, choices }];
    }),
    orphaned_current: response.orphaned_current && !refused(response.orphaned_current)
      ? withAllowedRoutes(response.orphaned_current)
      : null,
  };
}

/** Readiness never borrows a default when an explicit orphaned choice exists. */
export function currentSelectorChoice(state: Pick<SelectorChoicesState, 'response' | 'stale' | 'error'>): SelectorChoice | null {
  if (state.stale || state.error || !state.response) return null;
  const response = state.response;
  const id = response.current_choice_id ?? response.default_choice_id;
  if (!id) return null;
  return response.orphaned_current?.choice_id === id ? response.orphaned_current
    : response.groups.flatMap((group) => group.choices).find((choice) => choice.choice_id === id) ?? null;
}

/** Form-level guidance; detailed setup instructions stay in the picker. */
export function selectorReadinessMessage(state: SelectorChoicesState): string | null {
  if (state.error) return 'Couldn’t load models';
  if (state.loading || state.stale || !state.response) return 'Loading models…';
  const choice = currentSelectorChoice(state);
  if (!choice) return state.response.groups.some((group) => group.choices.length > 0)
    ? 'Choose a model' : 'No models available';
  if (choice.can_run) return null;
  if (choice.status === 'needs_setup') return 'Setup needed';
  if (choice.status === 'working') return 'Model setup in progress…';
  return choice.blocker?.message ?? `${choice.label} is unavailable.`;
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
