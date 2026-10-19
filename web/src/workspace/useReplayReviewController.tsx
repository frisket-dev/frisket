import { useCallback, useMemo, useState } from 'react';
import type { CellValue, SheetMeta } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useProjectDataResource } from '../bind/useRouteHandle';
import type { ColumnAnnotation } from '../grid/columnAnnotations';
import {
  advanceAfterAction,
  buildReplayPendingAnnotation,
  firstPendingRowId,
  replayPendingChipLabel,
} from '../grid/replayPending';
import { ReplayAcceptPopover } from './ReplayAcceptPopover';

interface ReplayReviewEntry {
  rowId: string;
  edit: CellValue;
  fresh: CellValue;
  hash: string;
  runId: string;
}
const REPLAY_REVIEW_PAGE_SIZE = 500;

/** Owns the transient replay queue; durable replay facts stay in project data. */
export function useReplayReviewController({
  sheet, refreshHistory, refreshSheets, showError,
}: {
  sheet: SheetMeta | undefined;
  refreshHistory: () => Promise<unknown>;
  refreshSheets: () => Promise<unknown>;
  showError: (message: string) => void;
}) {
  const { projectApi } = useWorkspaceStores();
  const projectData = useProjectDataResource();
  const [replayReview, setReplayReview] = useState<{
    columnId: string;
    columnName: string;
    entries: ReplayReviewEntry[];
    activeRowId: string | null;
  } | null>(null);
  const [replayBusy, setReplayBusy] = useState(false);

  const loadReplayPending = useCallback(
    async (columnId: string): Promise<ReplayReviewEntry[]> => {
      if (!sheet) return [];
      const page = await projectApi.getSheetData(sheet.id, 0, REPLAY_REVIEW_PAGE_SIZE, {
        columnIds: [columnId], valueMode: 'bounded',
      });
      const entries: ReplayReviewEntry[] = [];
      for (const row of page.rows) {
        const pending = row.replayPending?.[columnId];
        if (pending) {
          entries.push({
            rowId: row.id,
            edit: row.cells[columnId] ?? null,
            fresh: pending.freshValue,
            hash: pending.generatedValueHash,
            runId: pending.runId,
          });
        }
      }
      return entries;
    },
    [projectApi, sheet],
  );

  const startReplayReview = useCallback(
    async (columnId: string, columnName: string) => {
      try {
        const entries = await loadReplayPending(columnId);
        if (entries.length === 0) return;
        const first = firstPendingRowId(entries.map((entry) => Number(entry.rowId)));
        setReplayReview({
          columnId,
          columnName,
          entries,
          activeRowId: first != null ? String(first) : entries[0].rowId,
        });
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [loadReplayPending, showError],
  );

  const refreshReplayAfterAction = useCallback(
    async (columnId: string, columnName: string, actedRowId: string) => {
      const orderedBefore = replayReview?.entries.map((entry) => Number(entry.rowId)) ?? [];
      const nextNum = advanceAfterAction(orderedBefore, Number(actedRowId));
      projectData.invalidate();
      void refreshHistory();
      // Refresh metadata before surfacing replay-pending counts.

      void refreshSheets();
      const entries = await loadReplayPending(columnId);
      if (entries.length === 0) {
        setReplayReview(null);
        return;
      }
      const stillPending = new Set(entries.map((entry) => entry.rowId));
      const nextId =
        nextNum != null && stillPending.has(String(nextNum))
          ? String(nextNum)
          : entries[0].rowId;
      setReplayReview({ columnId, columnName, entries, activeRowId: nextId });
    },
    [replayReview, loadReplayPending, projectData, refreshHistory, refreshSheets],
  );

  const acceptActiveReplay = useCallback(async () => {
    if (!sheet || !replayReview?.activeRowId) return;
    const { columnId, columnName, activeRowId } = replayReview;
    setReplayBusy(true);
    try {
      const entry = replayReview.entries.find((candidate) => candidate.rowId === activeRowId);
      if (!entry) return;
      await projectApi.acceptReplayValue(
        sheet.id, columnId, activeRowId, entry.hash, entry.runId,
      );
      await refreshReplayAfterAction(columnId, columnName, activeRowId);
    } catch (e: unknown) {
      showError(e instanceof Error ? e.message : String(e));
    } finally {
      setReplayBusy(false);
    }
  }, [projectApi, sheet, replayReview, refreshReplayAfterAction, showError]);

  const keepActiveReplay = useCallback(async () => {
    if (!sheet || !replayReview?.activeRowId) return;
    const { columnId, columnName, activeRowId, entries } = replayReview;
    const entry = entries.find((candidate) => candidate.rowId === activeRowId);
    if (!entry) return;
    setReplayBusy(true);
    try {
      await projectApi.dismissReplayPending(sheet.id, columnId, activeRowId, entry.hash, entry.runId);
      await refreshReplayAfterAction(columnId, columnName, activeRowId);
    } catch (e: unknown) {
      showError(e instanceof Error ? e.message : String(e));
    } finally {
      setReplayBusy(false);
    }
  }, [projectApi, sheet, replayReview, refreshReplayAfterAction, showError]);

  const acceptAllReplay = useCallback(
    async (columnId: string) => {
      if (!sheet) return;
      setReplayBusy(true);
      try {
        await projectApi.acceptReplayValuesInColumn(sheet.id, columnId);
        projectData.invalidate();
        void refreshHistory();
        void refreshSheets();
        setReplayReview(null);
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      } finally {
        setReplayBusy(false);
      }
    },
    [projectApi, sheet, projectData, refreshHistory, refreshSheets, showError],
  );

  const dismissColumnReplay = useCallback(
    async (columnId: string) => {
      if (!sheet) return;
      try {
        const entries = await loadReplayPending(columnId);
        await Promise.all(
          entries.map((entry) =>
            projectApi.dismissReplayPending(
              sheet.id,
              columnId,
              entry.rowId,
              entry.hash,
              entry.runId,
            ),
          ),
        );
        projectData.invalidate();
        void refreshHistory();
        void refreshSheets();
        setReplayReview(null);
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [projectApi, sheet, loadReplayPending, projectData, refreshHistory, refreshSheets, showError],
  );

  const replayColumnAnnotations = useMemo<ColumnAnnotation[]>(() => {
    if (!sheet) return [];
    const annotations: ColumnAnnotation[] = [];
    for (const column of sheet.columns) {
      const count = column.replayPendingCount;
      const latestRunId = column.latestRunId;
      if (!count || latestRunId == null) continue;
      annotations.push(
        buildReplayPendingAnnotation({
          columnId: column.id,
          runId: Number(latestRunId),
          message: (
            <span className="replay-pending-chip">
              <span>{replayPendingChipLabel(count)}</span>
              <button
                type="button"
                className="replay-pending-review"
                data-testid="replay-review-button"
                onClick={() => void startReplayReview(column.id, column.name)}
              >
                Review
              </button>
              <button
                type="button"
                className="replay-pending-accept-all"
                data-testid="replay-accept-all-button"
                onClick={() => void acceptAllReplay(column.id)}
              >
                Accept all
              </button>
            </span>
          ),
          onDismiss: () => void dismissColumnReplay(column.id),
        }),
      );
    }
    return annotations;
  }, [sheet, startReplayReview, acceptAllReplay, dismissColumnReplay]);

  const replayReviewEntry = replayReview?.activeRowId
    ? replayReview.entries.find((entry) => entry.rowId === replayReview.activeRowId)
    : undefined;

  const replayReviewPopover = useMemo(() => replayReview && replayReviewEntry ? (
    <ReplayAcceptPopover
      columnName={replayReview.columnName}
      yourEdit={replayReviewEntry.edit}
      newerGenerated={replayReviewEntry.fresh}
      busy={replayBusy}
      onAccept={() => void acceptActiveReplay()}
      onKeep={() => void keepActiveReplay()}
      onClose={() => setReplayReview(null)}
    />
  ) : null, [replayReview, replayReviewEntry, replayBusy, acceptActiveReplay, keepActiveReplay]);
  return { replayColumnAnnotations, replayReviewPopover };
}
