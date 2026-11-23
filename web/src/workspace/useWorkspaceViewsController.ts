import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { ProjectInfo, View, SheetMeta, GridSortDirection } from '../api/open';
import { canEditProject } from '../api/projectRole';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useViewsHandle } from '../bind/useViewsHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useAlertCheckLinkHandle } from '../bind/useAlertCheckLinkHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { applyViewTransition, clearViewSelectionTransition } from '../state/workspaceTransitions';
import { VIEWS_CONTRIBUTION_ID, ALERTS_CONTRIBUTION_ID } from './contributionIds';
import {
  normalizeGridFilterSpec, normalizeGridSortSpec, normalizeColumnOrder,
  exactViewHiddenColumns, columnOrderKey, hiddenColumnsKey,
} from './gridColumnState';
import { gridFromSelection, sameViewDefinition, presentationFromGrid, gridFromPresentation } from './viewDefinition';
import { groupManagedHiddenColumnNames } from './workspaceColumnVisibility';
import type { useWorkspaceChromeState } from './useWorkspaceChromeState';
import type { useWorkbenchContributionVisibility } from './useWorkbenchContributionVisibility';

/** Saved-view mutations retain their existing editor and confirmation tickets. */
export function useWorkspaceViewsController({
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
  const viewsStore = useViewsHandle();
  const gridView = useGridViewHandle();
  const detail = useDetailHandle();
  const selection = useSelectionHandle();
  const alertCheckLink = useAlertCheckLinkHandle();
  const columnGroupSpecs = useSelector(gridView.store, (state) => state.columnGroupSpecs);
  const { openDiscover } = chromeState;
  const { isContributionHidden, revealContribution } = visibility;
  const views = useSelector(viewsStore.store, (s) => s.views);
  const viewName = useSelector(viewsStore.store, (s) => s.viewName);
  const viewsEditor = useSelector(viewsStore.store, (s) => s.editor);
  const viewsSaving = useSelector(viewsStore.store, (s) => s.isSaving);
  const viewsConfirmation = useSelector(viewsStore.store, (s) => s.confirmation);
  const viewsConfirmationError = useSelector(viewsStore.store, (s) => s.confirmationError);
  const viewsLastPublication = useSelector(viewsStore.store, (s) => s.lastPublication);
  const activeViewId = useSelector(gridView.store, (s) => s.activeViewId);

  useEffect(() => {
    if (activeViewId !== null && !views.some((view) => view.id === activeViewId)) {
      gridView.clearActiveView(activeViewId);
    }
  }, [activeViewId, gridView, views]);

  const currentViewsSheetId = sheet?.id ?? null;
  const canCreateView = currentViewsSheetId !== null && canEditProject(project);

  const loadViewsForSheet = useCallback(
    async (sheetId: string) => {
      const numericSheetId = Number(sheetId);
      const storeSheetId = Number.isFinite(numericSheetId) ? numericSheetId : sheetId;
      const token = viewsStore.beginViewsLoad(storeSheetId);
      try {
        const loadedViews = (await projectApi.listViews()).filter((view) => view.sheet_id === null || String(view.sheet_id) === sheetId);
        viewsStore.receiveViews(storeSheetId, token, loadedViews);
      } catch (error: unknown) {
        if (viewsStore.isViewsLoadCurrent(storeSheetId, token)) {
          showError(error instanceof Error ? error.message : String(error));
        }
      }
    },
    [projectApi, viewsStore, showError],
  );

  useEffect(() => {
    if (currentViewsSheetId === null) {
      viewsStore.resetForSheet(null);
      return;
    }
    const numericSheetId = Number(currentViewsSheetId);
    viewsStore.resetForSheet(
      Number.isFinite(numericSheetId) ? numericSheetId : currentViewsSheetId,
    );
    void loadViewsForSheet(currentViewsSheetId);
  }, [currentViewsSheetId, loadViewsForSheet, viewsStore]);

  useEffect(() => {
    const refresh = () => { if (currentViewsSheetId) void loadViewsForSheet(currentViewsSheetId); };
    window.addEventListener('frisket:views-changed', refresh);
    return () => window.removeEventListener('frisket:views-changed', refresh);
  }, [currentViewsSheetId, loadViewsForSheet]);

  const ensureViewsLoaded = useCallback(() => {
    if (currentViewsSheetId === null) return Promise.resolve();
    return loadViewsForSheet(currentViewsSheetId);
  }, [currentViewsSheetId, loadViewsForSheet]);

  const startCreatingView = useCallback(
    (defaultName = '') => {
      if (!canCreateView) return;
      viewsStore.startCreating();
      if (defaultName) viewsStore.setViewName(defaultName);
    },
    [canCreateView, viewsStore],
  );

  const revealViewsContribution = useCallback(() => {
    if (isContributionHidden(VIEWS_CONTRIBUTION_ID)) {
      revealContribution(VIEWS_CONTRIBUTION_ID);
    }
  }, [isContributionHidden, revealContribution]);

  const openViews = useCallback(() => {
    revealViewsContribution();
    openDiscover('Views');
    void ensureViewsLoaded();
  }, [ensureViewsLoaded, openDiscover, revealViewsContribution]);

  const openNewView = useCallback(
    (defaultName = '') => {
      if (!canCreateView) return;
      startCreatingView(defaultName);
      openViews();
    },
    [canCreateView, openViews, startCreatingView],
  );

  const currentSelection = useCurrentViewSelection(sheet);
  const currentViewDefinition = useCallback(() => {
    if (!currentSelection || !sheet) throw new Error('Choose a sheet first.');
    return {
      selection: currentSelection,
      presentation: presentationFromGrid(sheet, currentViewColumns, columnGroupSpecs),
    };
  }, [currentSelection, currentViewColumns, columnGroupSpecs, sheet]);
  const activeView = views.find((view) => view.id === activeViewId);
  const currentDefinition = useMemo(() => sheet ? currentViewDefinition() : null, [sheet, currentViewDefinition]);
  const viewModified = !!activeView?.spec && !!currentDefinition && !sameViewDefinition(activeView.spec, currentDefinition);

  const saveCurrentView = useCallback(() => {
    const name = viewName.trim();
    if (!sheet || !name) return;
    const ticket = viewsStore.beginEditorMutation();
    if (!ticket) return;
    void projectApi
      .saveView({ name, ...currentViewDefinition() })
      .then((saved) => {
        viewsStore.completeEditorMutation(ticket, saved);
      })
      .catch((e: Error) => {
        viewsStore.failEditorMutation(ticket);
        showError(e.message);
      });
  }, [currentViewDefinition, projectApi, viewsStore, sheet, showError, viewName]);

  const renameView = useCallback(() => {
    const name = viewName.trim();
    if (viewsEditor?.kind !== 'edit' || !name) return;
    const ticket = viewsStore.beginEditorMutation();
    if (!ticket) return;
    void projectApi
      .renameView(viewsEditor.view.id, { name })
      .then((updated) => {
        viewsStore.completeEditorMutation(ticket, updated);
      })
      .catch((e: Error) => {
        viewsStore.failEditorMutation(ticket);
        showError(e.message);
      });
  }, [projectApi, viewsStore, viewsEditor, showError, viewName]);

  const startViewDefinitionUpdate = useCallback(
    (view: View) => {
      if (!canCreateView || view.sheet_id === null || String(view.sheet_id) !== sheet?.id) return;
      viewsStore.openDefinitionUpdate(view);
    },
    [canCreateView, viewsStore, sheet?.id],
  );

  const replaceViewDefinition = useCallback(() => {
    const confirmation = viewsStore.store.get().confirmation;
    if (confirmation?.kind !== 'update-definition' || !canCreateView || !sheet) return;
    const ticket = viewsStore.beginConfirmationMutation();
    if (!ticket) return;
    void projectApi
      .replaceViewDefinition(confirmation.view.id, currentViewDefinition())
      .then((updated) => {
        viewsStore.completeDefinitionUpdate(ticket, updated);
      })
      .catch((error: unknown) => {
        viewsStore.failConfirmationMutation(
          ticket,
          error instanceof Error ? error.message : String(error),
        );
      });
  }, [canCreateView, currentViewDefinition, projectApi, viewsStore, sheet]);

  const startViewDelete = useCallback(
    (view: View) => {
      if (!canEditProject(project)) return;
      viewsStore.openDeleteConfirmation(view);
    },
    [project, viewsStore],
  );

  const deleteView = useCallback(() => {
    const confirmation = viewsStore.store.get().confirmation;
    if (confirmation?.kind !== 'delete' || !canCreateView) return;
    const ticket = viewsStore.beginConfirmationMutation();
    if (!ticket) return;
    void projectApi
      .deleteView(confirmation.view.id)
      .then(() => {
        viewsStore.completeDeleteMutation(ticket);
      })
      .catch((error: unknown) => {
        viewsStore.failConfirmationMutation(
          ticket,
          error instanceof Error ? error.message : String(error),
        );
      });
  }, [canCreateView, projectApi, viewsStore]);

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
      openDiscover('Alerts');
    } catch (error) { showError(error instanceof Error ? error.message : String(error)); }
  }, [activeView, currentViewDefinition, ensureViewsLoaded, isContributionHidden, openDiscover, project, projectApi, revealContribution, sheet, showError, alertCheckLink]);

  const applyView = useCallback(
    (view: View) => {
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

      applyViewTransition({ gridView, detail, selection }, sheet.id, {
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

  const openView = useCallback(async (viewId: number) => {
    const token = ++openGeneration.current;
    const openingSheet = sheet?.id;
    const isCurrent = () => token === openGeneration.current && route.store.get().sheetId === openingSheet;
    try {
      const view = (await projectApi.listViews(openingSheet)).find((item) => item.id === viewId);
      if (!isCurrent()) return;
      if (!view) throw new Error('This view is unavailable.');
      applyView(view);
      await ensureViewsLoaded();
    } catch (error) { if (isCurrent()) showError(error instanceof Error ? error.message : String(error)); }
  }, [projectApi, sheet?.id, route, applyView, ensureViewsLoaded, showError]);

  const clearViewSelection = useCallback(() => {
    openGeneration.current += 1;
    if (sheet) clearViewSelectionTransition({ gridView, detail, selection }, sheet.id);
  }, [gridView, detail, selection, sheet]);

  const selectionSheetId = sheet?.id ?? null;
  const updateViewSelection = useCallback((next: import('../api/types').ViewSelection) => {
    if (route.store.get().sheetId !== selectionSheetId) return;
    if (next.order.every((item) => 'score' in item)) gridView.clearSort();
    gridView.setSelection(next);
  }, [gridView, route, selectionSheetId]);

  return useMemo(() => ({
    views, viewName, viewModified, currentDefinition,
    updateViewSelection, clearViewSelection, viewsEditor, viewsSaving, viewsConfirmation,
    viewsConfirmationError, viewsLastPublication, activeViewId, canCreateView,
    ensureViewsLoaded, startCreatingView, openViews, openNewView, saveCurrentView,
    renameView, startViewDefinitionUpdate, replaceViewDefinition,
    startViewDelete, deleteView, addAlertForCurrentView, applyView, openView,
  }), [
    views, viewName, viewModified, currentDefinition, updateViewSelection, clearViewSelection, viewsEditor, viewsSaving, viewsConfirmation,
    viewsConfirmationError, viewsLastPublication, activeViewId, canCreateView,
    ensureViewsLoaded, startCreatingView, openViews, openNewView, saveCurrentView,
    renameView, startViewDefinitionUpdate, replaceViewDefinition,
    startViewDelete, deleteView, addAlertForCurrentView, applyView, openView,
  ]);
}
