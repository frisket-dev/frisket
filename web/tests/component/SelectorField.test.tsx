// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { HttpSelectorChoicesResponse } from '../../src/api/selectorChoices';
import { SelectorField } from '../../src/engine-selector/SelectorField';

function response(): HttpSelectorChoicesResponse {
  return {
    schema_version: 'frisket.selector_choices.v1',
    project_id: 'project-a',
    subject: { kind: 'action', action_id: 'map.classify', field: 'model' },
    depends_on: [],
    current_choice_id: 'ready-model',
    default_choice_id: 'ready-model',
    orphaned_current: null,
    groups: [{
      group_id: 'local',
      label: 'Local',
      choices: [{
        choice_id: 'ready-model',
        label: 'Ready model',
        summary: '',
        description: '',
        model_card_url: null,
        authored_selection: { kind: 'model', model: 'ready-model' },
        resolved_target: null,
        processing_destination: { label: 'Local' },
        facts: [],
        status: 'ready',
        can_author: true,
        can_run: true,
        blocker: null,
        setup: null,
        active_operation: null,
        is_default: true,
        is_current: true,
      }],
    }],
  } as HttpSelectorChoicesResponse;
}

describe('SelectorField', () => {
  it('loads authoritative readiness while its trigger is disabled for an active run', async () => {
    const load = vi.fn().mockResolvedValue(response());
    const onCurrentChoiceChange = vi.fn();
    render(
      <SelectorField
        projectId="project-a"
        label="Model"
        query={{
          schema_version: 'frisket.selector_choices_query.v1',
          subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} },
        }}
        recentNamespace="project-a:action:map.classify:model"
        disabled
        load={load}
        onSelect={vi.fn()}
        onCurrentChoiceChange={onCurrentChoiceChange}
      />,
    );

    await waitFor(() => expect(onCurrentChoiceChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ choice_id: 'ready-model', can_run: true }),
    ));
    expect(load).toHaveBeenCalledWith('project-a', expect.any(Object), expect.any(Object));
    expect(screen.getByRole('button', { name: /ready model/i })).toBeDisabled();
  });
});
