// @vitest-environment jsdom
// Refresh uses each sheet's saved settings. Deterministic work is free;
// paid steps retain the ordinary estimate/confirmation flow.

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
  it('describes saved settings without promising replacement or charging free work', () => {
    render(
      <CascadeConfirmDialog
        staleDeepestFirst={[staleSheet('2', 'People'), staleSheet('3', 'Places')]}
        onClose={() => {}}
        refreshSheets={() => {}}
      />,
    );

    expect(screen.getByTestId('cascade-rerun-scope')).toHaveTextContent(
      'Refresh these sheets from current sources using their saved settings.',
    );
    expect(screen.getByTestId('cascade-rerun-scope')).toHaveTextContent(
      'Paid steps may require confirmation.',
    );
    expect(screen.getByTestId('cascade-rerun-scope')).not.toHaveTextContent('every row is replaced');
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
    sheetId: 1,
    viewId: 4,
    schedule: '@hourly',
  detectionPolicy: { kind: 'conditions', entered: true, exited: false, count_changed: false, fields: [] },
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
      resolvedQuery: { similarity: { kind: 'embedding_similarity', embedding_index_id: 'idx-1' } },
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
      'Refresh the embedding index, then check the whole alert again',
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
          skills: true,
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
