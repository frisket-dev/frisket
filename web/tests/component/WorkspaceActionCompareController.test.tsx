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
import type { GeneratedActionDraft } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import type { ActionCompareUseSettings } from '../../src/workbench/actionCompareSession';
import { useWorkspaceActionCompareController } from '../../src/workspace/useWorkspaceActionCompareController';
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
}: {
  fingerprint?: string;
  runId?: number;
  draft?: GeneratedActionDraft;
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
    sample_row_ids: [11, 12, 13],
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

function renderController(selectedRowIds: readonly string[] = ['11', '12', '13']) {
  if (!harness) throw new Error('Controller harness is not initialized.');
  const current = harness;
  return renderHook(() => useWorkspaceActionCompareController({
    projectId: 'action-compare-controller',
    sheetId: '7',
    selectedRowIds,
    sampleOptions: {},
    runActionFromSurface: current.runActionFromSurface,
    closeActionRoute: current.closeActionRoute,
    setOpenSplit: current.stores.chrome.setOpenSplit,
    showError: current.showError,
  }), { wrapper: current.wrapper });
}

describe('useWorkspaceActionCompareController', () => {
  it('keeps the comparison selection separate from an all-row chosen draft and permits a labeled stale handoff', async () => {
    const initial = comparisonContext();
    const changed = comparisonContext({ fingerprint: 'sample-v2' });
    vi.mocked(harness!.api.getContext)
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(changed);
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

    const chosenDraft: GeneratedActionDraft = {
      ...ALL_ROWS_DRAFT,
      params: { ...ALL_ROWS_DRAFT.params, model: 'test/model-b' },
    };
    const chosen: ActionCompareUseSettings = {
      variantId: 'variant-b',
      variantLabel: 'B',
      draft: chosenDraft,
      context: snapshot.context,
      capturedSelection: snapshot.capturedSelection,
      sampleRowIds: [...snapshot.context.sample_row_ids],
      comparisonStale: false,
    };
    await act(async () => view.result.current.useActionCompareSettings(chosen));

    expect(harness!.runActionFromSurface).toHaveBeenCalledWith('map.extract', undefined, {
      actionDraft: chosenDraft,
      actionCompare: expect.objectContaining({
        kind: 'use-comparison-variant',
        variantId: 'variant-b',
        variantLabel: 'B',
        comparisonStale: true,
        context: expect.objectContaining({ sheet: { id: 7, name: 'Court filings' } }),
        capturedSelection: { rowIds: [11, 12, 13], totalCount: 3 },
        sampleRowIds: [11, 12, 13],
        returnToCompare: true,
      }),
    });
    expect(chosenDraft.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
    expect(harness!.showError).not.toHaveBeenCalled();
  });

  it('loads actual run settings and anchors the launch to that run and source sheet', async () => {
    const olderDraft: GeneratedActionDraft = {
      ...ALL_ROWS_DRAFT,
      params: { ...ALL_ROWS_DRAFT.params, model: 'test/older-model', instruction: 'Older run settings' },
    };
    vi.mocked(harness!.api.getContext).mockResolvedValue(
      comparisonContext({ runId: 42, draft: olderDraft }),
    );
    const view = renderController(['31', '32']);

    await act(async () => view.result.current.editActionRun({
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
    act(() => view.result.current.openActionCompareHistory());
    expect(view.result.current.actionCompareHistoryAnchor).toEqual({
      sheetId: '7', columnId: '101', runId: '42',
    });
  });

  it('warns only for scratch retargets, then disposes the old session before installing the new one', async () => {
    const first = comparisonContext();
    const secondDraft = { ...ALL_ROWS_DRAFT, params: { ...ALL_ROWS_DRAFT.params, instruction: 'Second' } };
    const second = comparisonContext({ fingerprint: 'sample-second', draft: secondDraft });
    vi.mocked(harness!.api.getContext).mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    const view = renderController();

    act(() => view.result.current.openActionCompare(ALL_ROWS_DRAFT));
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBeNull());
    const oldSession = view.result.current.actionCompareSession!;
    act(() => { oldSession.addVariant(); });

    act(() => view.result.current.openActionCompare(secondDraft));
    expect(view.result.current.actionComparePendingTarget).not.toBeNull();
    expect(harness!.api.getContext).toHaveBeenCalledTimes(1);
    act(() => view.result.current.cancelActionCompareRetarget());
    expect(view.result.current.actionComparePendingTarget).toBeNull();
    expect(view.result.current.actionCompareSession).toBe(oldSession);

    act(() => view.result.current.openActionCompare(secondDraft));
    act(() => view.result.current.confirmActionCompareRetarget());
    await waitFor(() => expect(view.result.current.actionCompareSession).not.toBe(oldSession));
    expect(oldSession.getSnapshot().disposed).toBe(true);
    expect(view.result.current.actionCompareSession!.getSnapshot().context.draft).toEqual(secondDraft);

    const active = view.result.current.actionCompareSession!;
    act(() => view.result.current.closeActionCompare());
    await waitFor(() => expect(active.getSnapshot().disposed).toBe(true));
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
});
