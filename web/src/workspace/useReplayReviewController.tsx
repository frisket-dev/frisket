import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CellValue, SheetMeta } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useProjectDataResource, useRouteHandle } from '../bind/useRouteHandle';
import type { ColumnAnnotation } from '../grid/columnAnnotations';
import {
  advanceAfterAction,
  firstPendingRowId,
  buildReplayPendingAnnotation,
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
interface ReplayReviewTarget {
  generation: number;
  sheetId: string;
  columnId: string;
  columnName: string;
  entries: ReplayReviewEntry[];
  activeRowId: string | null;
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
  const route = useRouteHandle();
  const [replayReview, setReplayReview] = useState<ReplayReviewTarget | null>(null);
  const [replayBusy, setReplayBusy] = useState(false);
  const [reviewSheetId, setReviewSheetId] = useState(sheet?.id);
  if (reviewSheetId !== sheet?.id) {
    setReviewSheetId(sheet?.id);
    setReplayReview(null);
    setReplayBusy(false);
  }
  const generation = useRef(0);
  useLayoutEffect(() => {
    generation.current += 1;
    return () => { generation.current += 1; };
  }, [sheet?.id]);
  const isCurrent = useCallback((sheetId: string, token: number) => (
    generation.current === token && route.store.get().sheetId === sheetId
  ), [route]);
  const closeReplayReview = useCallback(() => {
    generation.current += 1;
    setReplayReview(null);
    setReplayBusy(false);
  }, []);

  const loadReplayPending = useCallback(
    async (sheetId: string, columnId: string): Promise<ReplayReviewEntry[]> => {
      const page = await projectApi.getSheetData(sheetId, 0, REPLAY_REVIEW_PAGE_SIZE, {
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
    [projectApi],
  );

  const startReplayReview = useCallback(
    async (columnId: string, columnName: string) => {
      if (!sheet || route.store.get().sheetId !== sheet.id) return;
      const sheetId = sheet.id;
      const token = ++generation.current;
      setReplayReview(null);
      setReplayBusy(false);
      try {
        const entries = await loadReplayPending(sheetId, columnId);
        if (!isCurrent(sheetId, token) || entries.length === 0) return;
        const first = firstPendingRowId(entries.map((entry) => Number(entry.rowId)));
        setReplayReview({
          generation: token,
          sheetId,
          columnId,
          columnName,
          entries,
          activeRowId: first != null ? String(first) : entries[0].rowId,
        });
      } catch (e: unknown) {
        if (isCurrent(sheetId, token)) showError(e instanceof Error ? e.message : String(e));
      }
    },
    [isCurrent, loadReplayPending, route, sheet, showError],
  );

  const refreshReplayAfterAction = useCallback(
    async (target: ReplayReviewTarget, actedRowId: string, token: number) => {
      const { sheetId, columnId, columnName } = target;
      const orderedBefore = target.entries.map((entry) => Number(entry.rowId));
      const nextNum = advanceAfterAction(orderedBefore, Number(actedRowId));
      projectData.invalidate();
      void refreshHistory();
      // Refresh metadata before surfacing replay-pending counts.

      void refreshSheets();
      if (!isCurrent(sheetId, token)) return;
      const entries = await loadReplayPending(sheetId, columnId);
      if (!isCurrent(sheetId, token)) return;
      if (entries.length === 0) {
        setReplayReview(null);
        return;
      }
      const stillPending = new Set(entries.map((entry) => entry.rowId));
      const nextId =
        nextNum != null && stillPending.has(String(nextNum))
          ? String(nextNum)
          : entries[0].rowId;
      setReplayReview({ generation: token, sheetId, columnId, columnName, entries, activeRowId: nextId });
    },
    [isCurrent, loadReplayPending, projectData, refreshHistory, refreshSheets],
  );

  const acceptActiveReplay = useCallback(async () => {
    if (!replayReview?.activeRowId || !isCurrent(replayReview.sheetId, replayReview.generation)) return;
    const { sheetId, columnId, activeRowId } = replayReview;
    const token = replayReview.generation;
    setReplayBusy(true);
    try {
      const entry = replayReview.entries.find((candidate) => candidate.rowId === activeRowId);
      if (!entry) return;
      await projectApi.acceptReplayValue(
        sheetId, columnId, activeRowId, entry.hash, entry.runId,
      );
      await refreshReplayAfterAction(replayReview, activeRowId, token);
    } catch (e: unknown) {
      if (isCurrent(sheetId, token)) showError(e instanceof Error ? e.message : String(e));
    } finally {
      if (isCurrent(sheetId, token)) setReplayBusy(false);
    }
  }, [isCurrent, projectApi, replayReview, refreshReplayAfterAction, showError]);

  const keepActiveReplay = useCallback(async () => {
    if (!replayReview?.activeRowId || !isCurrent(replayReview.sheetId, replayReview.generation)) return;
    const { sheetId, columnId, activeRowId, entries } = replayReview;
    const token = replayReview.generation;
    const entry = entries.find((candidate) => candidate.rowId === activeRowId);
    if (!entry) return;
    setReplayBusy(true);
    try {
      await projectApi.dismissReplayPending(sheetId, columnId, activeRowId, entry.hash, entry.runId);
      await refreshReplayAfterAction(replayReview, activeRowId, token);
    } catch (e: unknown) {
      if (isCurrent(sheetId, token)) showError(e instanceof Error ? e.message : String(e));
    } finally {
      if (isCurrent(sheetId, token)) setReplayBusy(false);
    }
  }, [isCurrent, projectApi, replayReview, refreshReplayAfterAction, showError]);

  const acceptAllReplay = useCallback(
    async (columnId: string) => {
      if (!sheet || route.store.get().sheetId !== sheet.id) return;
      const sheetId = sheet.id;
      closeReplayReview();
      const token = generation.current;
      setReplayBusy(true);
      try {
        await projectApi.acceptReplayValuesInColumn(sheetId, columnId);
        projectData.invalidate();
        void refreshHistory();
        void refreshSheets();
      } catch (e: unknown) {
        if (isCurrent(sheetId, token)) showError(e instanceof Error ? e.message : String(e));
      } finally {
        if (isCurrent(sheetId, token)) setReplayBusy(false);
      }
    },
    [closeReplayReview, isCurrent, projectApi, route, sheet, projectData, refreshHistory, refreshSheets, showError],
  );

  const dismissColumnReplay = useCallback(
    async (columnId: string) => {
      if (!sheet || route.store.get().sheetId !== sheet.id) return;
      const sheetId = sheet.id;
      closeReplayReview();
      const token = generation.current;
      try {
        const entries = await loadReplayPending(sheetId, columnId);
        await Promise.all(
          entries.map((entry) =>
            projectApi.dismissReplayPending(
              sheetId,
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
      } catch (e: unknown) {
        if (isCurrent(sheetId, token)) showError(e instanceof Error ? e.message : String(e));
      }
    },
    [closeReplayReview, isCurrent, projectApi, route, sheet, loadReplayPending, projectData, refreshHistory, refreshSheets, showError],
  );

  const replayColumnAnnotations = useMemo<ColumnAnnotation[]>(() => {
    if (!sheet) return [];
    const annotations: ColumnAnnotation[] = [];
    for (const column of sheet.columns) {
      const count = column.replayPendingCount;
      const latestRunId = column.latestRunId;
      if (!count || latestRunId == null) continue;
      annotations.push(
        // The annotation stores event callbacks; it never invokes them during rendering.
        // eslint-disable-next-line react-hooks/refs
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

  const replayReviewPopover = useMemo(() => replayReview && replayReview.sheetId === sheet?.id && replayReviewEntry ? (
    <ReplayAcceptPopover
      columnName={replayReview.columnName}
      yourEdit={replayReviewEntry.edit}
      newerGenerated={replayReviewEntry.fresh}
      busy={replayBusy}
      onAccept={() => void acceptActiveReplay()}
      onKeep={() => void keepActiveReplay()}
      onClose={closeReplayReview}
    />
  ) : null, [replayReview, replayReviewEntry, replayBusy, acceptActiveReplay, closeReplayReview, keepActiveReplay, sheet?.id]);
  return { replayColumnAnnotations, replayReviewPopover };
}
