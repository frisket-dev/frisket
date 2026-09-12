import { useEffect, useMemo, type Ref } from 'react';

import {
  EngineSelector,
  type EngineSelectorChoice,
  type EngineSelectorGroup,
} from '../components/engine-selector';
import type { HttpSelectorChoicesQuery, SelectorChoice } from '../api/selectorChoices';
import { SelectorSetup } from './SelectorSetup';
import { useSelectorChoices, type SelectorChoicesLoader } from './useSelectorChoices';

export interface SelectorFieldProps {
  projectId: string | null | undefined;
  label: string;
  query: HttpSelectorChoicesQuery;
  recentNamespace: string;
  onSelect(choice: SelectorChoice): void;
  disabled?: boolean;
  testId?: string;
  load?: SelectorChoicesLoader;
  triggerRef?: Ref<HTMLButtonElement>;
  /** Host-specific policy may further narrow offered choices, never broaden them. */
  allowChoice?(choice: SelectorChoice): boolean;
  onCurrentChoiceChange?(choice: SelectorChoice | null): void;
}

function displayFact(choice: SelectorChoice): EngineSelectorChoice['facts'] {
  return choice.facts.map((fact) => {
    if (fact.kind === 'list') return { label: fact.label, value: fact.values };
    if (fact.kind === 'rate') {
      return { label: fact.label, value: `${fact.amount} ${fact.currency} / ${fact.unit}` };
    }
    return { label: fact.label, value: fact.value };
  });
}

function displayChoice(choice: SelectorChoice): EngineSelectorChoice {
  return {
    id: choice.choice_id,
    label: choice.label,
    summary: choice.summary || undefined,
    description: choice.description || undefined,
    facts: displayFact(choice),
    destination: choice.processing_destination.label,
    modelCardUrl: choice.model_card_url ?? undefined,
    status: choice.status,
    canAuthor: choice.can_author,
    canRun: choice.can_run,
    blocker: choice.blocker?.message,
    isDefault: choice.is_default,
  };
}

function selectorGroups(
  response: ReturnType<typeof useSelectorChoices>['response'],
  allowChoice: (choice: SelectorChoice) => boolean,
): EngineSelectorGroup[] {
  if (!response) return [];
  const groups = response.groups.map((group) => ({
    id: group.group_id,
    label: group.label,
    choices: group.choices.filter((choice) => choice.is_current || allowChoice(choice)).map(displayChoice),
  })).filter((group) => group.choices.length > 0);
  const orphan = response.orphaned_current;
  if (orphan && !groups.some((group) => group.choices.some((choice) => choice.id === orphan.choice_id))) {
    groups.push({ id: 'orphaned-current', label: 'Saved selection', choices: [displayChoice(orphan)] });
  }
  return groups;
}

function selectorQueryKey(
  query: HttpSelectorChoicesQuery,
  response: ReturnType<typeof useSelectorChoices>['response'],
): string {
  const subject = query.subject;
  if (subject.kind === 'action') {
    const names = new Set([subject.field, 'engine', 'model', ...(response?.depends_on ?? [])]);
    const params = Object.fromEntries(Object.entries(subject.params ?? {}).filter(([name]) => names.has(name)));
    return JSON.stringify({ kind: subject.kind, action_id: subject.action_id, field: subject.field, params });
  }
  return JSON.stringify(subject);
}

/**
 * Binds the visual selector to one authoritative project-scoped projection.
 * Callers receive the opaque authored selection from the raw server choice.
 */
export function SelectorField({
  projectId,
  label,
  query,
  recentNamespace,
  onSelect,
  disabled = false,
  testId,
  load,
  triggerRef,
  allowChoice = () => true,
  onCurrentChoiceChange,
}: SelectorFieldProps) {
  const state = useSelectorChoices({
    projectId,
    query,
    queryKey: selectorQueryKey,
    enabled: !disabled,
    load,
  });
  const groups = useMemo(() => selectorGroups(state.response, allowChoice), [allowChoice, state.response]);
  const choicesById = useMemo(() => new Map(
    state.response?.groups.flatMap((group) => group.choices).map((choice) => [choice.choice_id, choice])
      ?? [],
  ), [state.response]);
  const currentChoiceId = state.response?.current_choice_id ?? state.response?.default_choice_id ?? null;
  const currentChoice = state.stale || !currentChoiceId
    ? null
    : choicesById.get(currentChoiceId)
      ?? (state.response?.orphaned_current?.choice_id === currentChoiceId
        ? state.response.orphaned_current : null);
  useEffect(() => {
    onCurrentChoiceChange?.(currentChoice);
  }, [currentChoice, onCurrentChoiceChange]);
  if (state.error && !state.response) {
    return <div data-testid={testId} className="engine-selector-field-error" role="alert">
      <p>Could not load choices. {state.error.message}</p>
      <button type="button" className="btn" onClick={state.refresh}>Retry</button>
    </div>;
  }
  if (state.loading || !state.response) {
    return <div data-testid={testId} className="engine-selector-field-loading" aria-live="polite">
      Loading choices…
    </div>;
  }
  if (groups.length === 0) {
    return <p data-testid={testId} className="form-hint">No choices are available for this field.</p>;
  }
  return <div data-testid={testId}>
    <EngineSelector
      label={label}
      groups={groups}
      value={currentChoiceId}
      recentNamespace={recentNamespace}
      disabled={disabled}
      triggerRef={triggerRef}
      onSelect={(choice) => {
        const raw = choicesById.get(choice.id)
          ?? (state.response?.orphaned_current?.choice_id === choice.id ? state.response.orphaned_current : undefined);
        if (raw) onSelect(raw);
      }}
      renderDetailFooter={({ choice, onEditingChange }) => {
        const raw = choicesById.get(choice.id)
          ?? (state.response?.orphaned_current?.choice_id === choice.id ? state.response.orphaned_current : undefined);
        return raw && projectId ? <SelectorSetup
          projectId={projectId}
          choice={raw}
          onChanged={state.refresh}
          onEditingChange={onEditingChange}
        /> : null;
      }}
    />
  </div>;
}
