// @vitest-environment jsdom
//
// Ruling 4 ("a retry is a resume") — honest labels on the re-run doors that
// CANNOT resume by construction. sheet.refresh replaces every derived row and
// a watch run is a full fresh evaluation; each door must say so BEFORE the
// click (the 402 cost gate then prices any model step — no new confirmation
// is added here, only scope honesty).
//
//   * the stale-cascade confirm (tab-strip stale pill + Lineage tab share it)
//     names that every row is replaced, not just the changed ones, AND that
//     re-running is a fresh purchase rather than a resume;
//   * the Watches panel's run and refresh-index buttons name the full fresh
//     check instead of reading like a cheap poke.
//
// (The SheetInfo popover's "Re-run now" carries the same scope sentence —
// data-testid sheet-info-rerun-scope — but lives module-private in App.tsx;
// the cascade dialog covers the shared sheet.refresh copy contract here.)

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listWatches: vi.fn(),
      getNotificationsSummary: vi.fn(),
    },
  };
});

import { CascadeConfirmDialog } from '../../src/workbench/LineagePanel';
import { WatchesPanel } from '../../src/components/WatchesPanel';

import type { SheetMeta, WatchInfo } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { canEditProject } from '../../src/api/projectRole';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';
import {
  defineEditionModule,
  EditionModuleProvider,
} from '../../src/editions/module';

const api = createProjectApi('p1');
const listWatches = vi.spyOn(api, 'listWatches');
const getNotificationsSummary = vi.spyOn(api, 'getNotificationsSummary');
const { render } = createWorkspaceTestHarness({
  projectId: 'canonical-project',
  api: { projectApi: api },
});

const mockApi = { listWatches, getNotificationsSummary };

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

function staleSheet(id: string, name: string): SheetMeta {
  return {
    id,
    name,
    rowCount: 3,
    columns: [],
    parent: { sheetId: '1', sheetName: 'Stories', viaAction: 'derive.table_from_list' },
    syncState: 'stale',
  };
}

describe('stale-cascade confirm — scope honesty (ruling 4)', () => {
  it('says every row is replaced before the click', () => {
    render(
      <CascadeConfirmDialog
        staleDeepestFirst={[staleSheet('2', 'People'), staleSheet('3', 'Places')]}
        onClose={() => {}}
        refreshSheets={() => {}}
      />,
    );

    expect(screen.getByTestId('cascade-rerun-scope')).toHaveTextContent(
      'every row is replaced and re-derived, not just the changed ones',
    );
    // The money half: without this the scope sentence still reads like a
    // resume that only pays for what changed.
    expect(screen.getByTestId('cascade-rerun-scope')).toHaveTextContent(
      'a new run, not a resume: every derived row in every sheet listed above'
        + ' is charged again',
    );
    // The confirm itself is unchanged — the honesty is copy, not a new gate.
    const confirm = screen.getByTestId('cascade-confirm-run');
    expect(confirm).toHaveTextContent('Re-run 2');
    expect(confirm).toHaveAttribute('type', 'button');
    expect(confirm).toHaveClass('btn', 'btn-primary');
  });
});

const EMPTY_SUMMARY = {
  schemaVersion: 'frisket.notifications_summary.v1',
  total: 0,
  unseen: 0,
  seen: 0,
  read: 0,
  acknowledged: 0,
  bySeverity: {},
  bySourceKind: {},
  bySourceRef: [],
};

function blockedEmbeddingWatch(): WatchInfo {
  return {
    id: 5,
    name: 'Leads',
    scope: 'sheet',
    sheetId: 1,
    query: { kind: 'embedding_similarity', embedding_index_id: 'idx-1' },
    queryVersion: null,
    queryHash: null,
    enabled: true,
    lastEvaluatedOp: 0,
    lastRunId: 3,
    lastStatus: 'error',
    createdAt: '2026-07-27 00:00:00',
    updatedAt: '2026-07-27 00:00:00',
    latestRun: {
      id: 3,
      watchId: 5,
      status: 'error',
      opCursorBefore: 0,
      opCursorAfter: 0,
      matchedRows: 0,
      newRows: 0,
      error: 'embedding index is stale',
      errorCode: 'embedding_index_stale',
      resolvedQueryHash: null,
      resolvedQuery: {},
      startedAt: '2026-07-27 00:00:00',
      finishedAt: null,
    },
  };
}

describe('Watches panel — re-run labels say what they buy (ruling 4)', () => {
  it('names the full fresh check on the run and refresh-index doors', async () => {
    mockApi.listWatches.mockResolvedValue([blockedEmbeddingWatch()]);
    mockApi.getNotificationsSummary.mockResolvedValue(EMPTY_SUMMARY);

    render(<WatchesPanel canEdit />);

    await waitFor(() => expect(screen.getByTestId('watch-item')).toBeInTheDocument());

    // The play button is icon-only: its accessible name is the pre-click copy.
    expect(screen.getByTestId('run-watch')).toHaveAccessibleName(
      'Run Leads — manual evaluation; a full fresh check, not a resume',
    );
    // The blocked-index remediation names BOTH purchases it makes.
    const refresh = screen.getByTestId('watch-refresh-index-5');
    expect(refresh).toHaveTextContent('Refresh index & re-run');
    expect(refresh).toHaveAttribute(
      'title',
      'Refresh the embedding index, then run the whole watch again',
    );
  });

  it('renders edition controls for editors, not viewers, without changing the default', async () => {
    mockApi.listWatches.mockResolvedValue([blockedEmbeddingWatch()]);
    mockApi.getNotificationsSummary.mockResolvedValue(EMPTY_SUMMARY);

    const defaultRender = render(<WatchesPanel canEdit />);
    await waitFor(() => expect(screen.getByTestId('watch-item')).toBeInTheDocument());
    expect(screen.queryByTestId('edition-watch-control')).not.toBeInTheDocument();
    defaultRender.unmount();

    const editionId = 'watch-control-test';
    const edition = defineEditionModule({
      descriptor: {
        id: editionId,
        capabilities: {
          configurableNotificationDestinations: true,
          configurableNotificationEmail: true,
          identity: true,
          team: true,
        },
      },
      WatchControlSlot: ({ projectId, watch, refreshWatches }) => (
        <button type="button" data-testid="edition-watch-control" onClick={refreshWatches}>
          Destination for {watch.name} in {projectId}
        </button>
      ),
    });
    const EditionWrapper = ({ children }: { children: ReactNode }) => (
      <EditionModuleProvider edition={edition}>{children}</EditionModuleProvider>
    );

    const viewer = { id: 'p1', name: 'Viewer project', role: 'viewer' } as const;
    const viewerRender = render(
      <WatchesPanel canEdit={canEditProject(viewer)} />,
      { wrapper: EditionWrapper },
    );
    await waitFor(() => expect(screen.getByTestId('watch-item')).toBeInTheDocument());
    expect(screen.queryByTestId('edition-watch-control')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-watch')).not.toBeInTheDocument();
    viewerRender.unmount();

    const editor = { id: 'p1', name: 'Editor project', role: 'editor' } as const;
    render(
      <WatchesPanel canEdit={canEditProject(editor)} />,
      { wrapper: EditionWrapper },
    );
    await waitFor(() => expect(screen.getByTestId('edition-watch-control')).toHaveTextContent(
      'Destination for Leads in canonical-project',
    ));
    fireEvent.click(screen.getByTestId('edition-watch-control'));
    await waitFor(() => expect(mockApi.listWatches).toHaveBeenCalledTimes(4));
  });
});
