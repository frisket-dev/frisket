// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';

import type { SelectorChoice } from '../../src/api/selectorChoices';
import type { ModelPullDto } from '../../src/api/types';
import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse } from '../../src/generated/openHttpContracts';
import { SelectorField } from '../../src/engine-selector/SelectorField';
import { installDialogPolyfill } from '../support/domPolyfills';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('../../src/api/httpContract', () => ({ httpContract: request }));
beforeAll(installDialogPolyfill);
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
  localStorage.clear();
});

const setupRef = 'engine-setup:parakeet-tdt.local-onnx@1';
const query: HttpSelectorChoicesQuery = {
  schema_version: 'frisket.selector_choices_query.v1',
  subject: { kind: 'action', action_id: 'transcribe', field: 'engine', params: {} },
};

function operation(completedBytes: number, done = false): ModelPullDto {
  return {
    schemaVersion: 'frisket.model_pull.v4', id: 7, operation_kind: 'engine_setup',
    display_name: 'Parakeet setup', model: setupRef, status: done ? 'done' : 'running',
    capabilities: { cancel: !done, retry: false, remove: false },
    endpoint_id: null, endpoint_origin: null, cancel_requested: false, error: null,
    created_at: '2026-09-12T00:00:00Z', started_at: '2026-09-12T00:00:01Z',
    finished_at: done ? '2026-09-12T00:00:03Z' : null, initiated_by: null,
    phase: null, completed_bytes: completedBytes, total_bytes: 1000,
    resolved_digest: null, resolved_size: done ? 1000 : null, artifact: null,
  };
}

function catalog(active: ModelPullDto | null = null, ready = false): HttpSelectorChoicesResponse {
  const choice: SelectorChoice = {
    choice_id: 'engine:parakeet-tdt', label: 'Parakeet', summary: 'Local speech recognition',
    description: '', facts: [], authored_selection: { kind: 'engine', engine: 'parakeet-tdt' },
    processing_destination: { kind: 'local', label: 'This computer' }, resolved_target: null,
    is_current: true, is_default: true, can_author: true, can_run: ready,
    status: ready ? 'ready' : active ? 'working' : 'needs_setup', blocker: null,
    active_operation: active, model_card_url: null,
    setup: ready ? null : { kind: 'engine_setup', scope: 'workspace', setup_ref: setupRef,
      can_mutate: true, can_start: !active, blocked_by_operation: null },
  };
  return {
    schema_version: 'frisket.selector_choices.v1', project_id: 'project-a',
    subject: { kind: 'action', action_id: 'transcribe', field: 'engine' }, depends_on: [],
    current_choice_id: choice.choice_id, default_choice_id: choice.choice_id,
    groups: [{ group_id: 'local', kind: 'local', label: 'Local', status: choice.status, choices: [choice] }],
    orphaned_current: null,
  };
}

it('refreshes a completed setup into a ready choice while keeping the pinned selector dialog open', async () => {
  vi.useFakeTimers();
  const started = operation(100);
  let resolveReady!: (response: HttpSelectorChoicesResponse) => void;
  const refreshed = new Promise<HttpSelectorChoicesResponse>((resolve) => { resolveReady = resolve; });
  const choices = vi.fn().mockResolvedValueOnce(catalog()).mockResolvedValueOnce(catalog(started))
    .mockReturnValueOnce(refreshed);
  const polls = vi.fn().mockResolvedValueOnce(operation(600)).mockResolvedValueOnce(operation(1000, true));
  request.mockImplementation((id: string) => {
    if (id === 'tenant.selector_choices.post') return choices();
    if (id === 'tenant.setup_model_engine.post') return Promise.resolve({ pull: started, deduplicated: false });
    if (id === 'tenant.get_model_pull.get') return polls();
    throw new Error(`Unexpected HTTP operation: ${id}`);
  });
  const select = vi.fn();
  await act(async () => {
    render(<SelectorField projectId="project-a" label="Engine" query={query}
      recentNamespace="setup-completion-proof" onSelect={select} />);
  });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Parakeet/ })); });
  const dialog = screen.getByTestId('engine-selector-dialog');
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /Parakeet/ })); });
  expect(select).toHaveBeenCalledOnce();
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Download and set up' })); });
  expect(choices).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId('engine-selector-dialog')).toBe(dialog);
  expect(dialog).toBeVisible();
  expect(dialog).toHaveAttribute('open');
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(within(dialog).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '60');
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(choices).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId('engine-selector-dialog')).toBe(dialog);
  expect(dialog).toHaveAttribute('open');
  expect(within(dialog).getByTestId('model-pull-done')).toHaveTextContent('Parakeet setup installed');
  expect(within(dialog).queryByText('Ready')).not.toBeInTheDocument();
  await act(async () => { resolveReady(catalog(null, true)); });
  expect(screen.getByTestId('engine-selector-dialog')).toBe(dialog);
  expect(dialog).toBeVisible();
  expect(dialog).toHaveAttribute('open');
  expect(within(dialog).getByText('Ready')).toBeVisible();
  expect(within(dialog).queryByTestId('model-pull-progress')).not.toBeInTheDocument();
  expect(select).toHaveBeenCalledOnce();
  expect(request).toHaveBeenCalledWith('tenant.setup_model_engine.post', expect.objectContaining({ body: { setup_ref: setupRef } }));
  expect(request).toHaveBeenCalledWith('tenant.selector_choices.post', expect.objectContaining({ pathParams: { pid: 'project-a' }, body: query, signal: expect.any(AbortSignal) }));
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(polls).toHaveBeenCalledTimes(2);
});


it('projects a durable operation name and byte progress on the closed selected field', async () => {
  request.mockResolvedValue(catalog(operation(380)));
  await act(async () => {
    render(<SelectorField projectId="project-a" label="Engine" query={query}
      recentNamespace="selected-operation-projection" onSelect={vi.fn()} />);
  });
  const trigger = screen.getByRole('button', { name: /Parakeet/ });
  expect(trigger).toHaveTextContent('Parakeet setup');
  expect(within(trigger).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '38');
  expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
});

it('refreshes authoritative readiness after setup finishes with the dialog closed, without reopening', async () => {
  vi.useFakeTimers();
  const started = operation(100);
  let finished = false;
  const choices = vi.fn(() => Promise.resolve(finished ? catalog(null, true) : catalog(started)));
  request.mockImplementation((id: string) => {
    if (id === 'tenant.selector_choices.post') return choices();
    if (id === 'tenant.setup_model_engine.post') return Promise.resolve({ pull: started, deduplicated: false });
    if (id === 'tenant.get_model_pull.get') return Promise.resolve(operation(finished ? 1000 : 100, finished));
    throw new Error(`Unexpected HTTP operation: ${id}`);
  });
  // The initial catalog precedes the install; subsequent reads expose its durable row.
  choices.mockResolvedValueOnce(catalog());
  const current = vi.fn();
  const select = vi.fn();
  await act(async () => {
    render(<SelectorField projectId="project-a" label="Engine" query={query}
      recentNamespace="closed-setup-completion" onSelect={select} onCurrentChoiceChange={current} />);
  });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Parakeet/ })); });
  const dialog = screen.getByTestId('engine-selector-dialog');
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /Parakeet/ })); });
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Download and set up' })); });
  expect(current).toHaveBeenLastCalledWith(expect.objectContaining({ can_run: false, status: 'working' }));
  await act(async () => { fireEvent(dialog, new Event('cancel', { cancelable: true })); });
  expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
  finished = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(current).toHaveBeenLastCalledWith(expect.objectContaining({ can_run: true, status: 'ready' }));
  expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
  expect(select).toHaveBeenCalledOnce();
});
