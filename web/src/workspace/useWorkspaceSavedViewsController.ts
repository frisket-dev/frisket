import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { ProjectInfo, SavedView, SheetMeta, GridSortDirection } from '../api/open';
import { canEditProject } from '../api/projectRole';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useSavedViewsHandle } from '../bind/useSavedViewsHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useAlertCheckLinkHandle } from '../bind/useAlertCheckLinkHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { applySavedViewTransition, clearViewSelectionTransition } from '../state/workspaceTransitions';
import { SAVED_VIEWS_CONTRIBUTION_ID, ALERTS_CONTRIBUTION_ID } from './contributionIds';
import {
  normalizeGridFilterSpec, normalizeGridSortSpec, normalizeColumnOrder,
  exactViewHiddenColumns, columnOrderKey, hiddenColumnsKey,
} from './gridColumnState';
import { gridFromSelection, sameViewDefinition, presentationFromGrid, gridFromPresentation } from './viewDefinition';
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
  const { projectApi, route } = useWorkspaceStores();
  const openGeneration = useRef(0);
  useEffect(() => () => { openGeneration.current += 1; }, [sheet?.id]);
  const projectData = useProjectDataResource();
  const savedViews = useSavedViewsHandle();
  const gridView = useGridViewHandle();
  const detail = useDetailHandle();
  const selection = useSelectionHandle();
  const alertCheckLink = useAlertCheckLinkHandle();
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
        const loadedViews = (await projectApi.listViews()).filter((view) => view.sheet_id === null || String(view.sheet_id) === sheetId);
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

  useEffect(() => {
    const refresh = () => { if (currentSavedViewsSheetId) void loadViewsForSheet(currentSavedViewsSheetId); };
    window.addEventListener('frisket:views-changed', refresh);
    return () => window.removeEventListener('frisket:views-changed', refresh);
  }, [currentSavedViewsSheetId, loadViewsForSheet]);

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

  const currentSelection = useCurrentViewSelection(sheet);
  const currentViewDefinition = useCallback(() => {
    if (!currentSelection || !sheet) throw new Error('Choose a sheet first.');
    return {
      selection: currentSelection,
      presentation: presentationFromGrid(sheet, currentViewColumns, columnGroupSpecs),
    };
  }, [currentSelection, currentViewColumns, columnGroupSpecs, sheet]);
  const activeView = views.find((view) => view.id === activeSavedViewId);
  const currentDefinition = useMemo(() => sheet ? currentViewDefinition() : null, [sheet, currentViewDefinition]);
  const viewModified = !!activeView?.spec && !!currentDefinition && !sameViewDefinition(activeView.spec, currentDefinition);

  const saveCurrentView = useCallback(() => {
    const name = viewName.trim();
    if (!sheet || !name) return;
    const ticket = savedViews.beginEditorMutation();
    if (!ticket) return;
    void projectApi
      .saveView({ name, ...currentViewDefinition() })
      .then((saved) => {
        savedViews.completeEditorMutation(ticket, saved);
      })
      .catch((e: Error) => {
        savedViews.failEditorMutation(ticket);
        showError(e.message);
      });
  }, [currentViewDefinition, projectApi, savedViews, sheet, showError, viewName]);

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
      if (!canEditProject(project)) return;
      savedViews.openDeleteConfirmation(view);
    },
    [project, savedViews],
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

  const addAlertForCurrentView = useCallback(async () => {
    if (!sheet || !canEditProject(project)) return;
    try {
      // Alerts follow a saved definition, never a copied transient filter.
      const view = activeView ?? await projectApi.saveView({ name: `View: ${sheet.name}`, ...currentViewDefinition() });
      await ensureViewsLoaded();
      const existing = (await projectApi.listAlerts()).some((alert) => alert.viewId === view.id);
      if (existing) alertCheckLink.clearPendingCreateDraft();
      else alertCheckLink.setPendingCreateDraft({ view_id: view.id, sheet_id: view.sheet_id, name: view.name });
      if (isContributionHidden(ALERTS_CONTRIBUTION_ID)) revealContribution(ALERTS_CONTRIBUTION_ID);
      openDiscover('Watches');
    } catch (error) { showError(error instanceof Error ? error.message : String(error)); }
  }, [activeView, currentViewDefinition, ensureViewsLoaded, isContributionHidden, openDiscover, project, projectApi, revealContribution, sheet, showError, alertCheckLink]);

  const applySavedView = useCallback(
    (view: SavedView) => {
      openGeneration.current += 1;
      if (!sheet || String(view.sheet_id) !== sheet.id) return;
      if (view.unavailable_reason || !view.spec) { showError(view.unavailable_reason ?? 'This view is unavailable.'); return; }
      let grid;
      try { grid = gridFromSelection(sheet, view.spec.selection); }
      catch (error) { showError(error instanceof Error ? error.message : String(error)); return; }
      const filter = normalizeGridFilterSpec(grid.filter);
      const sort = normalizeGridSortSpec(grid.sort);
      const { columns: rawColumns, groups } = gridFromPresentation(sheet, view.spec.presentation);
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
        scopeRowIds: view.spec.selection.scope.kind === 'sheet' ? view.spec.selection.scope.row_ids : undefined,
        selection: view.spec.selection,
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
    [detail, gridView, project.id, projectData, selection, sheet, showError],
  );

  const openSavedView = useCallback(async (viewId: number) => {
    const token = ++openGeneration.current;
    const openingSheet = sheet?.id;
    const isCurrent = () => token === openGeneration.current && route.store.get().sheetId === openingSheet;
    try {
      const view = (await projectApi.listViews(openingSheet)).find((item) => item.id === viewId);
      if (!isCurrent()) return;
      if (!view) throw new Error('This view is unavailable.');
      applySavedView(view);
      await ensureViewsLoaded();
    } catch (error) { if (isCurrent()) showError(error instanceof Error ? error.message : String(error)); }
  }, [projectApi, sheet?.id, route, applySavedView, ensureViewsLoaded, showError]);

  const clearViewSelection = useCallback(() => {
    openGeneration.current += 1;
    if (sheet) clearViewSelectionTransition({ gridView, detail, selection }, sheet.id);
  }, [gridView, detail, selection, sheet]);

  const updateViewSelection = useCallback((next: import('../api/types').ViewSelection) => {
    if (next.order.every((item) => 'score' in item)) gridView.clearSort();
    gridView.setSelection(next);
  }, [gridView]);

  return useMemo(() => ({
    views, viewName, viewModified, currentDefinition,
    updateViewSelection, clearViewSelection, savedViewsEditor, savedViewsSaving, savedViewsConfirmation,
    savedViewsConfirmationError, savedViewsLastPublication, activeSavedViewId, canCreateSavedView,
    ensureViewsLoaded, startCreatingSavedView, openSavedViews, openNewSavedView, saveCurrentView,
    renameSavedView, startSavedViewDefinitionUpdate, replaceSavedViewDefinition,
    startSavedViewDelete, deleteView, addAlertForCurrentView, applySavedView, openSavedView,
  }), [
    views, viewName, viewModified, currentDefinition, updateViewSelection, clearViewSelection, savedViewsEditor, savedViewsSaving, savedViewsConfirmation,
    savedViewsConfirmationError, savedViewsLastPublication, activeSavedViewId, canCreateSavedView,
    ensureViewsLoaded, startCreatingSavedView, openSavedViews, openNewSavedView, saveCurrentView,
    renameSavedView, startSavedViewDefinitionUpdate, replaceSavedViewDefinition,
    startSavedViewDelete, deleteView, addAlertForCurrentView, applySavedView, openSavedView,
  ]);
}
