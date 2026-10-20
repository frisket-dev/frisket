import { useCallback, useEffect, useMemo } from 'react';
import type { ProjectInfo, SavedView, SheetMeta, GridSortDirection } from '../api/open';
import { canEditProject } from '../api/projectRole';
import type { ColumnGroupViewSpec } from '../grid/SheetGrid';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useSavedViewsHandle } from '../bind/useSavedViewsHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useWatchRunLinkHandle } from '../bind/useWatchRunLinkHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { applySavedViewTransition } from '../state/workspaceTransitions';
import { SAVED_VIEWS_CONTRIBUTION_ID, WATCHES_CONTRIBUTION_ID } from './contributionIds';
import {
  normalizeGridFilterSpec, normalizeGridSortSpec, normalizeColumnOrder,
  exactViewHiddenColumns, columnOrderKey, hiddenColumnsKey,
} from './gridColumnState';
import { groupManagedHiddenColumnNames } from './workspaceColumnVisibility';
import type { useWorkspaceChromeState } from './useWorkspaceChromeState';
import type { useWorkbenchContributionVisibility } from './useWorkbenchContributionVisibility';

/** Saved-view mutations retain their existing editor and confirmation tickets. */
export function useWorkspaceSavedViewsController({
  project, sheet, currentViewColumns, chromeState, visibility, showError,
}: {
  project: ProjectInfo;
  sheet: SheetMeta | undefined;
  currentViewColumns: string[] | null;
  chromeState: Pick<ReturnType<typeof useWorkspaceChromeState>, 'openDiscover'>;
  visibility: Pick<ReturnType<typeof useWorkbenchContributionVisibility>, 'isContributionHidden' | 'revealContribution'>;
  showError: (message: string) => void;
}) {
  const { projectApi } = useWorkspaceStores();
  const projectData = useProjectDataResource();
  const savedViews = useSavedViewsHandle();
  const gridView = useGridViewHandle();
  const detail = useDetailHandle();
  const selection = useSelectionHandle();
  const watchRunLink = useWatchRunLinkHandle();
  const activeGridFilter = useSelector(gridView.store, (state) => state.applied.filter);
  const activeGridSort = useSelector(gridView.store, (state) => state.applied.sort);
  const scopeRowIds = useSelector(gridView.store, (state) => state.applied.scopeRowIds ?? null);
  const columnGroupSpecs = useSelector(gridView.store, (state) => state.columnGroupSpecs);
  const { openDiscover } = chromeState;
  const { isContributionHidden, revealContribution } = visibility;
  const views = useSelector(savedViews.store, (s) => s.views);
  const viewName = useSelector(savedViews.store, (s) => s.viewName);
  const savedViewsEditor = useSelector(savedViews.store, (s) => s.editor);
  const savedViewsSaving = useSelector(savedViews.store, (s) => s.isSaving);
  const savedViewsConfirmation = useSelector(savedViews.store, (s) => s.confirmation);
  const savedViewsConfirmationError = useSelector(savedViews.store, (s) => s.confirmationError);
  const savedViewsLastPublication = useSelector(savedViews.store, (s) => s.lastPublication);
  const activeSavedViewId = useSelector(gridView.store, (s) => s.activeSavedViewId);

  useEffect(() => {
    if (activeSavedViewId !== null && !views.some((view) => view.id === activeSavedViewId)) {
      gridView.clearActiveSavedView(activeSavedViewId);
    }
  }, [activeSavedViewId, gridView, views]);

  const currentSavedViewsSheetId = sheet?.id ?? null;
  const canCreateSavedView = currentSavedViewsSheetId !== null && canEditProject(project);

  const loadViewsForSheet = useCallback(
    async (sheetId: string) => {
      const numericSheetId = Number(sheetId);
      const storeSheetId = Number.isFinite(numericSheetId) ? numericSheetId : sheetId;
      const token = savedViews.beginViewsLoad(storeSheetId);
      try {
        const loadedViews = await projectApi.listViews(sheetId);
        savedViews.receiveViews(storeSheetId, token, loadedViews);
      } catch (error: unknown) {
        if (savedViews.isViewsLoadCurrent(storeSheetId, token)) {
          showError(error instanceof Error ? error.message : String(error));
        }
      }
    },
    [projectApi, savedViews, showError],
  );

  useEffect(() => {
    if (currentSavedViewsSheetId === null) {
      savedViews.resetForSheet(null);
      return;
    }
    const numericSheetId = Number(currentSavedViewsSheetId);
    savedViews.resetForSheet(
      Number.isFinite(numericSheetId) ? numericSheetId : currentSavedViewsSheetId,
    );
    void loadViewsForSheet(currentSavedViewsSheetId);
  }, [currentSavedViewsSheetId, loadViewsForSheet, savedViews]);

  const ensureViewsLoaded = useCallback(() => {
    if (currentSavedViewsSheetId === null) return Promise.resolve();
    return loadViewsForSheet(currentSavedViewsSheetId);
  }, [currentSavedViewsSheetId, loadViewsForSheet]);

  const startCreatingSavedView = useCallback(
    (defaultName = '') => {
      if (!canCreateSavedView) return;
      savedViews.startCreating();
      if (defaultName) savedViews.setViewName(defaultName);
    },
    [canCreateSavedView, savedViews],
  );

  const revealSavedViewsContribution = useCallback(() => {
    if (isContributionHidden(SAVED_VIEWS_CONTRIBUTION_ID)) {
      revealContribution(SAVED_VIEWS_CONTRIBUTION_ID);
    }
  }, [isContributionHidden, revealContribution]);

  const openSavedViews = useCallback(() => {
    revealSavedViewsContribution();
    openDiscover('Views');
    void ensureViewsLoaded();
  }, [ensureViewsLoaded, openDiscover, revealSavedViewsContribution]);

  const openNewSavedView = useCallback(
    (defaultName = '') => {
      if (!canCreateSavedView) return;
      startCreatingSavedView(defaultName);
      openSavedViews();
    },
    [canCreateSavedView, openSavedViews, startCreatingSavedView],
  );

  const currentViewInput = useCallback(
    (name: string, sheetId: string | number) => ({
      name,
      sheetId,
      filter: activeGridFilter ?? {},
      scope_row_ids: scopeRowIds,
      sort: activeGridSort ?? null,
      columns: currentViewColumns,
      column_groups: columnGroupSpecs.length > 0 ? columnGroupSpecs : null,
    }),
    [activeGridFilter, activeGridSort, columnGroupSpecs, currentViewColumns, scopeRowIds],
  );

  // Definition replacement is intentionally a complete capture.  Unlike
  // creation's convenience defaults, this shape has no omitted field whose
  // old persisted value could accidentally survive an explicit update.
  const currentViewDefinition = useCallback(
    () => ({
      filter: activeGridFilter ?? {},
      scope_row_ids: scopeRowIds,
      sort: activeGridSort ?? null,
      columns: currentViewColumns,
      column_groups: columnGroupSpecs.length > 0 ? columnGroupSpecs : null,
    }),
    [activeGridFilter, activeGridSort, columnGroupSpecs, currentViewColumns, scopeRowIds],
  );

  const saveCurrentView = useCallback(() => {
    const name = viewName.trim();
    if (!sheet || !name) return;
    const ticket = savedViews.beginEditorMutation();
    if (!ticket) return;
    void projectApi
      .saveView(currentViewInput(name, sheet.id))
      .then((saved) => {
        savedViews.completeEditorMutation(ticket, saved);
      })
      .catch((e: Error) => {
        savedViews.failEditorMutation(ticket);
        showError(e.message);
      });
  }, [currentViewInput, projectApi, savedViews, sheet, showError, viewName]);

  const renameSavedView = useCallback(() => {
    const name = viewName.trim();
    if (savedViewsEditor?.kind !== 'edit' || !name) return;
    const ticket = savedViews.beginEditorMutation();
    if (!ticket) return;
    void projectApi
      .renameView(savedViewsEditor.view.id, { name })
      .then((updated) => {
        savedViews.completeEditorMutation(ticket, updated);
      })
      .catch((e: Error) => {
        savedViews.failEditorMutation(ticket);
        showError(e.message);
      });
  }, [projectApi, savedViews, savedViewsEditor, showError, viewName]);

  const startSavedViewDefinitionUpdate = useCallback(
    (view: SavedView) => {
      if (!canCreateSavedView || view.sheet_id === null || String(view.sheet_id) !== sheet?.id) return;
      savedViews.openDefinitionUpdate(view);
    },
    [canCreateSavedView, savedViews, sheet?.id],
  );

  const replaceSavedViewDefinition = useCallback(() => {
    const confirmation = savedViews.store.get().confirmation;
    if (confirmation?.kind !== 'update-definition' || !canCreateSavedView || !sheet) return;
    const ticket = savedViews.beginConfirmationMutation();
    if (!ticket) return;
    void projectApi
      .replaceViewDefinition(confirmation.view.id, currentViewDefinition())
      .then((updated) => {
        savedViews.completeDefinitionUpdate(ticket, updated);
      })
      .catch((error: unknown) => {
        savedViews.failConfirmationMutation(
          ticket,
          error instanceof Error ? error.message : String(error),
        );
      });
  }, [canCreateSavedView, currentViewDefinition, projectApi, savedViews, sheet]);

  const startSavedViewDelete = useCallback(
    (view: SavedView) => {
      if (!canCreateSavedView || view.sheet_id === null || String(view.sheet_id) !== sheet?.id) return;
      savedViews.openDeleteConfirmation(view);
    },
    [canCreateSavedView, savedViews, sheet?.id],
  );

  const deleteView = useCallback(() => {
    const confirmation = savedViews.store.get().confirmation;
    if (confirmation?.kind !== 'delete' || !canCreateSavedView) return;
    const ticket = savedViews.beginConfirmationMutation();
    if (!ticket) return;
    void projectApi
      .deleteView(confirmation.view.id)
      .then(() => {
        savedViews.completeDeleteMutation(ticket);
      })
      .catch((error: unknown) => {
        savedViews.failConfirmationMutation(
          ticket,
          error instanceof Error ? error.message : String(error),
        );
      });
  }, [canCreateSavedView, projectApi, savedViews]);

  const addWatchForCurrentView = useCallback(() => {
    if (!sheet || !canEditProject(project)) return;
    const definition = currentViewInput('', sheet.id);
    const sheetId = Number(definition.sheetId);
    watchRunLink.setPendingCreateDraft({
      query: {
        kind: 'filter',
        sheet_id: sheetId,
        filter: definition.filter,
      },
      scope: { kind: 'sheet', sheet_id: sheetId },
      name: `Watch: ${sheet.name}`,
    });
    if (isContributionHidden(WATCHES_CONTRIBUTION_ID)) {
      revealContribution(WATCHES_CONTRIBUTION_ID);
    }
    openDiscover('Watches');
  }, [currentViewInput, isContributionHidden, openDiscover, project, revealContribution, sheet, watchRunLink]);

  const applySavedView = useCallback(
    (view: SavedView) => {
      if (!sheet || String(view.sheet_id) !== sheet.id) return;
      const filter = normalizeGridFilterSpec(view.spec.filter);
      const sort = normalizeGridSortSpec(view.spec.sort);
      const rawColumns = Array.isArray(view.spec.columns)
        ? view.spec.columns.filter((name): name is string => typeof name === 'string')
        : null;
      const groups = Array.isArray(view.spec.column_groups)
        ? (view.spec.column_groups as ColumnGroupViewSpec[])
        : [];
      const layouts = Object.fromEntries(
        groups.map((group) => [
          String(group.run_id),
          {
            label: group.label,
            showConfidence: group.show_confidence,
            showJustification: group.show_justification,
          },
        ]),
      );
      const key = `frisket:column-groups:${project.id}:${sheet.id}`;
      if (groups.length === 0) {
        localStorage.removeItem(key);
      } else {
        localStorage.setItem(key, JSON.stringify(layouts));
      }
      const columns = rawColumns === null
        ? normalizeColumnOrder(null, sheet)
        : normalizeColumnOrder(rawColumns, sheet);
      const hiddenColumns = rawColumns === null
        ? []
        : (() => {
            const groupHidden = groupManagedHiddenColumnNames(groups);
            return exactViewHiddenColumns(sheet, rawColumns, groupHidden);
          })();
      if (rawColumns === null) {
        localStorage.removeItem(columnOrderKey(project.id, sheet.id));
        localStorage.removeItem(hiddenColumnsKey(project.id, sheet.id));
      } else {
        localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(columns));
        localStorage.setItem(hiddenColumnsKey(project.id, sheet.id), JSON.stringify(hiddenColumns));
      }
      const firstColumnName = sheet.columns[0]?.name ?? '';
      let draftSortColumn = firstColumnName;
      let draftSortDirection: GridSortDirection = 'asc';
      if (sort?.[0]) {
        draftSortColumn = sort[0].column;
        draftSortDirection = sort[0].dir;
      }

      applySavedViewTransition({ gridView, detail, selection }, sheet.id, {
        viewId: view.id,
        sheetId: sheet.id,
        scopeRowIds: view.spec.scope_row_ids as number[] | undefined,
        filter,
        sort,
        draftSortColumn,
        draftSortDirection,
        columns,
        hiddenColumns,
        columnGroupSpecs: groups,
      });
      projectData.invalidate();
    },
    [detail, gridView, project.id, projectData, selection, sheet],
  );

  return useMemo(() => ({
    views, viewName, savedViewsEditor, savedViewsSaving, savedViewsConfirmation,
    savedViewsConfirmationError, savedViewsLastPublication, activeSavedViewId, canCreateSavedView,
    ensureViewsLoaded, startCreatingSavedView, openSavedViews, openNewSavedView, saveCurrentView,
    renameSavedView, startSavedViewDefinitionUpdate, replaceSavedViewDefinition,
    startSavedViewDelete, deleteView, addWatchForCurrentView, applySavedView,
  }), [
    views, viewName, savedViewsEditor, savedViewsSaving, savedViewsConfirmation,
    savedViewsConfirmationError, savedViewsLastPublication, activeSavedViewId, canCreateSavedView,
    ensureViewsLoaded, startCreatingSavedView, openSavedViews, openNewSavedView, saveCurrentView,
    renameSavedView, startSavedViewDefinitionUpdate, replaceSavedViewDefinition,
    startSavedViewDelete, deleteView, addWatchForCurrentView, applySavedView,
  ]);
}
