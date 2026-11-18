// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ActionParamResolution, RunEstimate } from '../../src/api/types';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { mockLocalProviders, mountTypedForm, type MountTypedFormOptions } from './cutoverUiF1TypedForm';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const outputs = [{ key: 'summary', column_type: 'text' }];
const valid = { diagnostics: {}, logical_outputs: outputs };
const knownEstimate = { cost: 0.01, rows: 2, billed_cost: 10_000, policy_id: 'frisket.identity.v1', promise_set_hash: 'old-display-only' };
let dispose: (() => void) | undefined;
beforeEach(() => { installPopoverPolyfill(); mockLocalProviders(); });
afterEach(() => { cleanup(); dispose?.(); vi.restoreAllMocks(); });

async function mount(options: Partial<MountTypedFormOptions> = {}) {
  const result = mountTypedForm({ kind: 'map.summarize',
    sheet: sheetMeta([columnDef({ id: '1', name: 'body', type: 'text' })], { id: '7', rowCount: 2 }),
    initialDraft: { action_id: 'map.summarize', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['body'], model: 'test/model', context: '' }, output_names: { summary: 'summary' } },
    resolveParams: async () => valid, estimateAction: async () => knownEstimate, ...options });
  dispose = result.dispose;
  await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$0.01'));
  expect(screen.getByTestId('generated-action-run')).toBeEnabled();
  return result;
}
const changeContext = (value: string) => fireEvent.change(screen.getByTestId('field-context'), { target: { value } });
const clickRun = () => fireEvent.click(screen.getByTestId('generated-action-run'));

it('debounces edits while retaining the previous amount and enabled Run through validation and quotation', async () => {
  const nextEstimate = deferred<RunEstimate>();
  const nextValidation = deferred<ActionParamResolution>();
  const { estimateAction, resolveParams } = await mount();
  estimateAction.mockImplementation(() => nextEstimate.promise);
  resolveParams.mockImplementation(() => nextValidation.promise);
  const previousCalls = estimateAction.mock.calls.length;
  for (const value of ['R', 'Re', 'Reporting']) {
    changeContext(value);
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('Previous estimate: $0.01');
    expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent('UNKNOWN');
  }
  await waitFor(() => expect(estimateAction).toHaveBeenCalledTimes(previousCalls + 1));
  expect(estimateAction.mock.lastCall?.[0].params.context).toBe('Reporting');
  expect(screen.getByTestId('generated-action-run')).toBeEnabled();
  await act(async () => { nextValidation.resolve(valid); nextEstimate.resolve({ ...knownEstimate, billed_cost: 20_000 }); });
  expect(screen.getByTestId('cost-estimate')).toHaveTextContent('Estimated cost: $0.02');
});

it('replaces a previous known price with a current unknown quote and ignores late obsolete quotes', async () => {
  const old = deferred<RunEstimate>();
  const current = deferred<RunEstimate>();
  const { estimateAction } = await mount();
  estimateAction.mockImplementation((request) => request.params.context === 'old' ? old.promise : current.promise);
  changeContext('old');
  await waitFor(() => expect(estimateAction.mock.lastCall?.[0].params.context).toBe('old'));
  changeContext('current');
  await waitFor(() => expect(estimateAction.mock.lastCall?.[0].params.context).toBe('current'));
  await act(async () => current.resolve({ cost: null, rows: 2, billed_cost: null }));
  expect(screen.getByTestId('cost-estimate')).toHaveTextContent('UNKNOWN');
  expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent('$0.01');
  await act(async () => old.resolve({ ...knownEstimate, billed_cost: 9_000_000 }));
  expect(screen.getByTestId('cost-estimate')).toHaveTextContent('UNKNOWN');
  expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent('$9.00');
});

it('Run awaits current validation and quotation, then executes only the exact latest unconfirmed params', async () => {
  const validation = deferred<ActionParamResolution>();
  const estimate = deferred<RunEstimate>();
  const { onExecute, resolveParams, estimateAction } = await mount();
  resolveParams.mockImplementation(() => validation.promise);
  estimateAction.mockImplementation(() => estimate.promise);
  changeContext('latest');
  clickRun();
  expect(onExecute).not.toHaveBeenCalled();
  expect(resolveParams.mock.lastCall?.[0].params.context).toBe('latest');
  await act(async () => validation.resolve(valid));
  expect(onExecute).not.toHaveBeenCalled();
  expect(estimateAction.mock.lastCall?.[0].params.context).toBe('latest');
  await act(async () => estimate.resolve({ ...knownEstimate, billed_cost: 50_000, promise_set_hash: 'new-display-only' }));
  expect(onExecute).toHaveBeenCalledTimes(1);
  expect(onExecute.mock.calls[0][0]).toMatchObject({ params: { context: 'latest' } });
  expect(onExecute.mock.calls[0][0]).not.toHaveProperty('confirmation');
});

it('discards an in-flight Run if its request changes before validation finishes', async () => {
  const validation = deferred<ActionParamResolution>();
  const { onExecute, resolveParams } = await mount();
  resolveParams.mockImplementation(() => validation.promise);
  changeContext('clicked');
  clickRun();
  fireEvent.change(screen.getByTestId('field-output-summary'), { target: { value: 'changed_destination' } });
  await act(async () => validation.resolve(valid));
  expect(onExecute).not.toHaveBeenCalled();
});

it('never executes when the latest validation refuses parameters', async () => {
  const validation = deferred<ActionParamResolution>();
  const { onExecute, resolveParams } = await mount();
  resolveParams.mockImplementation(() => validation.promise);
  changeContext('refused');
  clickRun();
  await act(async () => validation.resolve({ ...valid,
    diagnostics: { context: { ok: false, message: 'Invalid context.' } } }));
  expect(onExecute).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
});

it('discards an in-flight Run when the form unmounts', async () => {
  const validation = deferred<ActionParamResolution>();
  const { onExecute, resolveParams, unmount } = await mount();
  resolveParams.mockImplementation(() => validation.promise);
  changeContext('clicked');
  clickRun();
  unmount();
  await act(async () => validation.resolve(valid));
  expect(onExecute).not.toHaveBeenCalled();
});
