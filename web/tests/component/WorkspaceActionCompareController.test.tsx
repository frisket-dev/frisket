// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { PropsWithChildren } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as actionComparisonApi from '../../src/api/actionComparison';
import type {
  ActionComparisonApi,
  ActionComparisonContext,
} from '../../src/api/actionComparison';
import { createProjectApi } from '../../src/api/real';
import type { ActionCatalogEntry, ColumnRun, GeneratedActionDraft } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import type { ActionCompareUseSettings } from '../../src/workbench/actionCompareSession';
import {
  createActionCompareCostConfirmation,
  useWorkspaceActionCompareController,
} from '../../src/workspace/useWorkspaceActionCompareController';
import { catalogPayload } from '../support/actionFormFixtures';
import { row } from '../support/domainFixtures';

const ALL_ROWS_DRAFT: GeneratedActionDraft = {
  action_id: 'map.extract',
  scope: { kind: 'sheet_rows', sheet_id: 7 },
  params: {
    source: ['story'],
    engine: 'llm',
    model: 'test/model-a',
    instruction: 'Extract the party.',
    fields: [{ name: 'party', type: 'text', description: 'Named party' }],
  },
  output_names: { party: 'pdf_party' },
};

function comparisonContext({
  fingerprint = 'sample-v1',
  runId,
  draft = ALL_ROWS_DRAFT,
  sampleRowIds = [11, 12, 13],
}: {
  fingerprint?: string;
  runId?: number;
  draft?: GeneratedActionDraft;
  sampleRowIds?: number[];
} = {}): ActionComparisonContext {
  const runBasis = runId === undefined ? null : {
    kind: 'run' as const,
    run_id: runId,
    column_id: 101,
  };
  return {
    schema_version: 'frisket.action_compare_context.v1',
    available: true,
    reason: null,
    limits: { max_sample_rows: 20, max_challengers: 3 },
    baseline_mode: runBasis ? 'current_values' : 'preview',
    comparison_basis: runBasis ?? { kind: 'draft', draft },
    draft,
    sheet: { id: 7, name: 'Court filings' },
    sample_row_ids: sampleRowIds,
    sample_fingerprint: fingerprint,
    locked_params: ['source', 'fields'],
    source_columns: [{
      param: 'source', column_id: 1, name: 'story', column_type: 'text', format: null,
    }],
    source_rows: Object.fromEntries([11, 12, 13].map(id => [String(id), {
      story: {
        state: 'value', value: `Story ${id}`,
        origin: { kind: 'source_cell', run_id: null, op_id: null, review_decision: null },
      },
    }])),
    output_columns: [{
      logical_key: 'party', column_id: runBasis ? 101 : null, current_name: 'pdf_party',
      column_type: 'text', format: null, hidden: false, schema: {},
    }],
    expected_output_signature: 'party:text',
    replacement_targets: runBasis
      ? [{ logical_key: 'party', current_name: 'pdf_party', column_id: 101 }]
      : [],
    baseline_rows: runBasis ? Object.fromEntries([11, 12, 13].map(id => [String(id), {
      party: {
        state: 'value', value: `Party ${id}`,
        origin: { kind: 'run_result', run_id: runId, op_id: id, review_decision: null },
      },
    }])) : null,
    origin_summary: {
      mixed_runs: false, includes_manual_edits: false, includes_reject_clear: false,
    },
  };
}

const RUN: ColumnRun = {
  runId: '42',
  actionKind: 'map.extract',
  actionName: 'Extract structured fields',
  model: 'test/model-a',
  status: 'completed',
  spec: {},
  totalRows: 240,
  completedRows: 240,
  failedRows: 0,
  cost: 0,
  startedAt: '2026-10-25T10:12:00Z',
  finishedAt: '2026-10-25T10:13:00Z',
  durationMs: 60_000,
  tokensIn: 10,
  tokensOut: 20,
  current: true,
  humanScore: { passed: 0, graded: 0 },
  judgeScores: [],
};

function chosenSettings(context: ActionComparisonContext): ActionCompareUseSettings {
  return {
    variantId: 'variant-b',
    variantLabel: 'B',
    draft: {
      ...ALL_ROWS_DRAFT,
      params: { ...ALL_ROWS_DRAFT.params, model: 'test/model-b' },
    },
    context,
    capturedSelection: { rowIds: [11, 12, 13], totalCount: 3 },
    sampleRowIds: [...context.sample_row_ids],
  };
}

interface Harness {
  stores: WorkspaceStores;
  api: ActionComparisonApi;
  runActionFromSurface: ReturnType<typeof vi.fn>;
  closeActionRoute: ReturnType<typeof vi.fn>;
  showError: ReturnType<typeof vi.fn>;
  wrapper: (props: PropsWithChildren) => JSX.Element;
}

let harness: Harness | null = null;

beforeEach(() => {
  const projectApi = createProjectApi('action-compare-controller');
  vi.spyOn(projectApi, 'getSheetData').mockResolvedValue({
    rows: [11, 12, 13].map((id, index) => row({ story: `Story ${id}` }, { id: String(id), index })),
    columns: [],
    total: 3,
  });
  const stores = createWorkspaceStores('action-compare-controller', projectApi);
  const api: ActionComparisonApi = {
    getContext: vi.fn(),
    startPreview: vi.fn(),
    getPreview: vi.fn(),
    cancelPreview: vi.fn().mockResolvedValue(undefined),
    discardPreview: vi.fn().mockResolvedValue(undefined),
  };
  vi.spyOn(actionComparisonApi, 'createActionComparisonApi').mockReturnValue(api);
  harness = {
    stores,
    api,
    runActionFromSurface: vi.fn(),
    closeActionRoute: vi.fn(),
    showError: vi.fn(),
    wrapper: ({ children }) => (
      <WorkspaceStoresContext.Provider value={stores}>{children}</WorkspaceStoresContext.Provider>
    ),
  };
});

afterEach(() => {
  cleanup();
  harness?.stores.dispose();
  harness = null;
  vi.restoreAllMocks();
});

function renderController(selectedRowIds: readonly string[] = ['11', '12', '13'],
  selectedRowCount = selectedRowIds.length) {
  if (!harness) throw new Error('Controller harness is not initialized.');
  const current = harness;
  return renderHook(() => useWorkspaceActionCompareController({
    projectId: 'action-compare-controller',
    sheetId: '7',
    selectedRowIds,
    selectedRowCount,
    sampleOptions: {},
    runActionFromSurface: current.runActionFromSurface,
    closeActionRoute: current.closeActionRoute,
    setOpenSplit: current.stores.chrome.setOpenSplit,
    showError: current.showError,
  }), { wrapper: current.wrapper });
}

describe('useWorkspaceActionCompareController', () => {
  it('cancels only its own pending cost gate when the comparison signal aborts', async () => {
    const jobs = harness!.stores.job;
    const confirm = createActionCompareCostConfirmation(jobs);
    const controller = new AbortController();
    const comparison = confirm({ cost: 1, rows: 3 }, 'Compare these rows?', controller.signal);

    expect(jobs.store.get().costGate?.message).toBe('Compare these rows?');
    controller.abort();
    await expect(comparison).resolves.toBe(false);
    expect(jobs.store.get().costGate).toBeNull();

    const displacedController = new AbortController();
    const displaced = confirm(
      { cost: 1, rows: 3 },
      'Displaced comparison?',
      displacedController.signal,
    );
    const successor = jobs.requestCostConfirmation({ cost: 2, rows: 5 }, 'Successor request?');
    displacedController.abort();

    await expect(displaced).resolves.toBe(false);
    expect(jobs.store.get().costGate?.message).toBe('Successor request?');
    jobs.confirmCostGate();
    await expect(successor).resolves.toBe(true);
  });

  it('keeps the comparison selection separate from an all-row chosen draft', async () => {
    const initial = comparisonContext();
    vi.mocked(harness!.api.getContext).mockResolvedValueOnce(initial);
    const view = renderController();

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());

    expect(harness!.api.getContext).toHaveBeenNthCalledWith(1, {
      source: { kind: 'draft', draft: ALL_ROWS_DRAFT },
      sample_row_ids: [11, 12, 13],
    });
    const snapshot = view.result.current.actionCompareSession!.getSnapshot();
    expect(snapshot.capturedSelection).toEqual({ rowIds: [11, 12, 13], totalCount: 3 });
    expect(snapshot.context.draft.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });

    const chosen = { ...chosenSettings(snapshot.context), capturedSelection: snapshot.capturedSelection };
    await act(async () => view.result.current.useActionCompareSettings(chosen));

    expect(harness!.runActionFromSurface).toHaveBeenCalledWith('map.extract', undefined, {
      actionDraft: chosen.draft,
      actionCompare: expect.objectContaining({
        kind: 'use-comparison-variant',
        variantId: 'variant-b',
        variantLabel: 'B',
        context: expect.objectContaining({ sheet: { id: 7, name: 'Court filings' } }),
        capturedSelection: { rowIds: [11, 12, 13], totalCount: 3 },
        sampleRowIds: [11, 12, 13],
        returnToCompare: true,
      }),
    });
    expect(harness!.api.getContext).toHaveBeenCalledTimes(1);
    expect(chosen.draft.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
    expect(harness!.showError).not.toHaveBeenCalled();
  });

  it('does not reload a snapshot when a Use-launched replacement run completes', async () => {
    const initial = comparisonContext({ runId: 41 });
    vi.mocked(harness!.api.getContext).mockResolvedValueOnce(initial);
    const view = renderController();

    act(() => view.result.current.compareActionRun({
      sheetId: '7', columnId: '101', runId: '41',
    }));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());
    await act(async () => view.result.current.useActionCompareSettings(
      chosenSettings(initial),
    ));

    const running = {
      runId: 'replacement-run',
      actionName: 'Extract structured fields',
      actionKind: 'map.extract',
      sheetId: '7',
      targetColumnId: '101',
      targetRowIds: ['11', '12', '13'],
      status: 'running' as const,
      completedRows: 0,
      totalRows: 3,
      failedRows: 0,
      costSoFar: 0,
    };
    act(() => harness!.stores.job.store.set(state => ({ ...state, run: running })));
    act(() => harness!.stores.job.store.set(state => ({
      ...state,
      run: { ...running, status: 'complete', completedRows: 3 },
    })));

    expect(harness!.api.getContext).toHaveBeenCalledTimes(1);
    expect(view.result.current.actionCompareSession!.getSnapshot().context).toBe(initial);
  });

  it('honors an explicit null comparison selection instead of recapturing the live grid selection', async () => {
    vi.mocked(harness!.api.getContext).mockResolvedValue(comparisonContext());
    const view = renderController(['91', '92']);

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT, null));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());

    expect(harness!.api.getContext).toHaveBeenCalledWith({
      source: { kind: 'draft', draft: ALL_ROWS_DRAFT },
      sample_row_ids: [11, 12, 13],
    });
    expect(harness!.stores.projectApi.getSheetData).toHaveBeenCalledWith(
      '7', 0, 20, expect.objectContaining({ columnIds: [], valueMode: 'bounded' }),
    );
    expect(view.result.current.actionCompareSession!.getSnapshot().capturedSelection).toBeNull();
  });

  it('refuses an incompletely loaded live selection without claiming a partial comparison scope', () => {
    const view = renderController(['91', '92'], 5);
    const partialDraft: GeneratedActionDraft = {
      ...ALL_ROWS_DRAFT,
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [91, 92] },
    };

    act(() => view.result.current.openActionCompare(partialDraft));

    expect(harness!.showError).toHaveBeenCalledWith(
      'Some selected rows are not loaded; choose a smaller selection.',
    );
    expect(harness!.api.getContext).not.toHaveBeenCalled();
    expect(view.result.current.actionCompareSession).toBeNull();
  });

  it('refuses Use while a retarget context read is pending and stays closed after retarget failure', async () => {
    const initial = comparisonContext();
    let rejectRetarget!: (error: Error) => void;
    vi.mocked(harness!.api.getContext)
      .mockResolvedValueOnce(initial)
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRetarget = reject; }));
    const view = renderController();

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());
    act(() => { view.result.current.actionCompareSession!.addVariant(); });

    const nextDraft = { ...ALL_ROWS_DRAFT, params: { ...ALL_ROWS_DRAFT.params, instruction: 'Next target' } };
    act(() => view.result.current.openActionCompare(nextDraft));
    act(() => view.result.current.confirmActionCompareRetarget());
    await waitFor(() => expect(harness!.api.getContext).toHaveBeenCalledTimes(2));

    await act(async () => view.result.current.useActionCompareSettings(chosenSettings(initial)));
    expect(harness!.api.getContext).toHaveBeenCalledTimes(2);
    expect(harness!.runActionFromSurface).not.toHaveBeenCalled();

    await act(async () => { rejectRetarget(new Error('Retarget context failed.')); });
    await waitFor(() => expect(harness!.showError).toHaveBeenCalledWith('Retarget context failed.'));
    expect(view.result.current.actionCompareSession).toBeNull();
    expect(harness!.stores.compareView.store.get().action.open).toBe(false);
    expect(harness!.runActionFromSurface).not.toHaveBeenCalled();
  });

  it('restores actual run settings and anchors the launch to that run and source sheet', async () => {
    const olderDraft: GeneratedActionDraft = {
      ...ALL_ROWS_DRAFT,
      params: { ...ALL_ROWS_DRAFT.params, model: 'test/older-model', instruction: 'Older run settings' },
    };
    vi.mocked(harness!.api.getContext).mockResolvedValue(
      comparisonContext({ runId: 42, draft: olderDraft }),
    );
    const view = renderController(['31', '32']);

    await act(async () => view.result.current.restoreActionRunSettings({
      sheetId: '7', columnId: '101', runId: '42',
    }));

    expect(harness!.runActionFromSurface).toHaveBeenCalledWith('map.extract', undefined, {
      actionDraft: olderDraft,
      actionCompare: expect.objectContaining({
        kind: 'edit-existing',
        context: expect.objectContaining({
          sheet: { id: 7, name: 'Court filings' },
          comparison_basis: { kind: 'run', run_id: 42, column_id: 101 },
        }),
        capturedSelection: { rowIds: [31, 32], totalCount: 2 },
        returnToCompare: false,
      }),
    });
    const trigger = document.createElement('button');
    act(() => view.result.current.openActionCompareHistory(trigger));
    expect(view.result.current.actionCompareHistoryMenuTarget).toEqual({
      runAnchor: { sheetId: '7', columnId: '101', runId: '42' },
      trigger,
    });
    act(() => view.result.current.closeActionCompareHistory());
    expect(view.result.current.actionCompareHistoryMenuTarget).toBeNull();
  });

  it('captures the current grid selection when restoring history beside an open comparison', async () => {
    vi.mocked(harness!.api.getContext).mockResolvedValue(comparisonContext({ runId: 42 }));
    let liveSelection: readonly string[] = ['11', '12'];
    const current = harness!;
    const view = renderHook(() => useWorkspaceActionCompareController({
      projectId: 'action-compare-controller',
      sheetId: '7',
      selectedRowIds: liveSelection,
      selectedRowCount: liveSelection.length,
      sampleOptions: {},
      runActionFromSurface: current.runActionFromSurface,
      closeActionRoute: current.closeActionRoute,
      setOpenSplit: current.stores.chrome.setOpenSplit,
      showError: current.showError,
    }), { wrapper: current.wrapper });

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());
    expect(view.result.current.actionCompareSession!.getSnapshot().capturedSelection?.rowIds)
      .toEqual([11, 12]);

    liveSelection = ['31', '32'];
    view.rerender();
    await act(async () => view.result.current.restoreActionRunSettings({
      sheetId: '7', columnId: '101', runId: '42',
    }));

    expect(current.runActionFromSurface).toHaveBeenCalledWith(
      'map.extract',
      undefined,
      expect.objectContaining({
        actionCompare: expect.objectContaining({
          kind: 'edit-existing',
          capturedSelection: { rowIds: [31, 32], totalCount: 2 },
        }),
      }),
    );
  });

  it('does not advertise comparison when canonical context loading refuses the run', async () => {
    const comparisonEntry = {
      kind: 'map.extract',
      ui_hints: { comparison: { kind: 'row_overlay' } },
    } as ActionCatalogEntry;
    act(() => harness!.stores.actionCatalog.store.set(current => ({
      ...current,
      status: 'ready',
      catalog: catalogPayload({ actions: [comparisonEntry] }),
      version: current.version + 1,
    })));
    vi.spyOn(harness!.stores.projectApi, 'getColumnRuns').mockResolvedValue({
      columnName: 'pdf_party',
      offset: 0,
      limit: 20,
      totalRuns: 1,
      hasMore: false,
      nextOffset: null,
      currentRun: RUN,
      currentRunLoaded: true,
      latestRun: RUN,
      latestRunLoaded: true,
      mixedOrigins: false,
      runs: [RUN],
    });
    vi.mocked(harness!.api.getContext).mockResolvedValue({
      ...comparisonContext({ runId: 42 }),
      available: false,
      reason: { code: 'source_deleted', message: 'A source column was deleted.' },
    });
    const view = renderController();

    const details = await view.result.current.loadActionRunEntry({
      sheetId: '7', columnId: '101', runId: '42',
    });

    expect(details).toEqual(expect.objectContaining({
      actionTitle: 'Extract structured fields',
      comparisonKind: null,
      outputLabels: ['pdf_party'],
    }));
  });

  it('surfaces a transient comparison-context failure so the grid can retry it', async () => {
    const comparisonEntry = {
      kind: 'map.extract',
      ui_hints: { comparison: { kind: 'row_overlay' } },
    } as ActionCatalogEntry;
    act(() => harness!.stores.actionCatalog.store.set(current => ({
      ...current,
      status: 'ready',
      catalog: catalogPayload({ actions: [comparisonEntry] }),
      version: current.version + 1,
    })));
    vi.spyOn(harness!.stores.projectApi, 'getColumnRuns').mockResolvedValue({
      columnName: 'pdf_party',
      offset: 0,
      limit: 20,
      totalRuns: 1,
      hasMore: false,
      nextOffset: null,
      currentRun: RUN,
      currentRunLoaded: true,
      latestRun: RUN,
      latestRunLoaded: true,
      mixedOrigins: false,
      runs: [RUN],
    });
    vi.mocked(harness!.api.getContext).mockRejectedValue(new Error('temporary network failure'));
    const view = renderController();

    await expect(view.result.current.loadActionRunEntry({
      sheetId: '7', columnId: '101', runId: '42',
    })).rejects.toThrow('temporary network failure');
  });

  it('warns only for scratch retargets, then detaches and disposes the old session before loading the new context', async () => {
    const first = comparisonContext();
    const secondDraft = { ...ALL_ROWS_DRAFT, params: { ...ALL_ROWS_DRAFT.params, instruction: 'Second' } };
    const second = comparisonContext({ fingerprint: 'sample-second', draft: secondDraft });
    vi.mocked(harness!.api.getContext).mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    const view = renderController();

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());
    const oldSession = view.result.current.actionCompareSession!;
    const dispose = oldSession.dispose.bind(oldSession);
    let finishDispose!: () => void;
    const disposalBlocked = new Promise<void>(resolve => { finishDispose = resolve; });
    vi.spyOn(oldSession, 'dispose').mockImplementation(async () => {
      await disposalBlocked;
      await dispose();
    });
    act(() => { oldSession.addVariant(); });

    act(() => view.result.current.openActionCompare(secondDraft));
    expect(view.result.current.actionComparePendingTarget).not.toBeNull();
    expect(harness!.api.getContext).toHaveBeenCalledTimes(1);
    act(() => view.result.current.cancelActionCompareRetarget());
    expect(view.result.current.actionComparePendingTarget).toBeNull();
    expect(view.result.current.actionCompareSession).toBe(oldSession);

    act(() => view.result.current.openActionCompare(secondDraft));
    act(() => view.result.current.confirmActionCompareRetarget());
    expect(view.result.current.actionCompareSession).toBeNull();
    expect(harness!.api.getContext).toHaveBeenCalledTimes(1);
    await act(async () => { finishDispose(); await disposalBlocked; });
    await waitFor(() => expect(harness!.api.getContext).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBe(oldSession));
    expect(oldSession.getSnapshot().disposed).toBe(true);
    expect(view.result.current.actionCompareSession!.getSnapshot().context.draft).toEqual(secondDraft);

    const active = view.result.current.actionCompareSession!;
    act(() => view.result.current.closeActionCompare());
    await waitFor(() => expect(active.getSnapshot().disposed).toBe(true));
    expect(view.result.current.actionCompareSession).toBeNull();
    expect(harness!.stores.compareView.store.get().action.open).toBe(false);
  });

  it('closes the obsolete comparison when history restoration supersedes a pending retarget', async () => {
    const initial = comparisonContext();
    const nextDraft = {
      ...ALL_ROWS_DRAFT,
      params: { ...ALL_ROWS_DRAFT.params, instruction: 'Retarget settings' },
    };
    let resolveRetarget!: (context: ActionComparisonContext) => void;
    let resolveEdit!: (context: ActionComparisonContext) => void;
    vi.mocked(harness!.api.getContext)
      .mockResolvedValueOnce(initial)
      .mockImplementationOnce(() => new Promise(resolve => { resolveRetarget = resolve; }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveEdit = resolve; }));
    const view = renderController();

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());
    const oldSession = view.result.current.actionCompareSession!;
    act(() => { oldSession.addVariant(); });
    act(() => view.result.current.openActionCompare(nextDraft));
    act(() => view.result.current.confirmActionCompareRetarget());
    await waitFor(() => expect(harness!.api.getContext).toHaveBeenCalledTimes(2));
    expect(oldSession.getSnapshot().disposed).toBe(true);
    expect(view.result.current.actionCompareSession).toBeNull();

    let restore!: Promise<void>;
    act(() => {
      restore = view.result.current.restoreActionRunSettings({
        sheetId: '7', columnId: '101', runId: '42',
      });
    });
    await waitFor(() => expect(harness!.api.getContext).toHaveBeenCalledTimes(3));
    expect(harness!.stores.compareView.store.get().action.open).toBe(false);
    await act(async () => {
      resolveRetarget(comparisonContext({ draft: nextDraft }));
      resolveEdit(comparisonContext({ runId: 42 }));
      await restore;
    });

    expect(harness!.runActionFromSurface).toHaveBeenCalledTimes(1);
    expect(view.result.current.actionCompareSession).toBeNull();
    expect(harness!.stores.compareView.store.get().action.open).toBe(false);
  });

  it('does not reopen comparison when a pending context read finishes after close', async () => {
    let resolveContext!: (context: ActionComparisonContext) => void;
    vi.mocked(harness!.api.getContext).mockImplementation(() => new Promise(resolve => {
      resolveContext = resolve;
    }));
    const view = renderController();

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(harness!.api.getContext).toHaveBeenCalled());
    act(() => view.result.current.closeActionCompare());
    await act(async () => { resolveContext(comparisonContext()); });

    expect(view.result.current.actionCompareSession).toBeNull();
    expect(harness!.stores.compareView.store.get().action.open).toBe(false);
  });

  it('does not focus a closed comparison when returning from Edit settings', async () => {
    vi.mocked(harness!.api.getContext).mockResolvedValue(comparisonContext());
    const view = renderController();
    const focus = vi.spyOn(harness!.stores.compareView, 'focus');

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());
    act(() => view.result.current.closeActionCompare());
    act(() => view.result.current.returnToActionCompare());

    expect(harness!.closeActionRoute).toHaveBeenCalledTimes(1);
    expect(focus).not.toHaveBeenCalled();
    expect(harness!.stores.compareView.store.get().action.active).toBe(false);
  });
});
