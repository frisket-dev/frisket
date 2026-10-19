import { useCallback, useEffect, useMemo } from 'react';
import { groupManagedHiddenColumnNames } from './workspaceColumnVisibility';
import type {
  ColumnDef, GridFilterBboxValue, GridFilterEntityValue, GridFilterOperator,
  GridFilterSpec, GridSortDirection, ProjectInfo, SheetMeta,
} from '../api/open';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useSavedViewsHandle } from '../bind/useSavedViewsHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkViewHandle } from '../bind/useWorkViewHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { useWorkspaceChromeState } from './useWorkspaceChromeState';
import {
  applyGridBboxFilterTransition, applyGridFilterTransition, applyGridSortTransition,
  clearGridFilterTransition, clearGridSortTransition, insertColumnBesideTransition,
  completeAddColumnTransition,
} from '../state/workspaceTransitions';
import { filterOperatorForColumnType, type HeaderMenuState } from './workspaceState';
import {
  clampFrozenColumnCount, columnOrderKey, exactViewHiddenColumns,
  filterConditionFromDraft, frozenColumnsKey, hasCustomColumnOrder,
  hiddenColumnsKey, shownDefaultColumnsKey, loadColumnOrder,
  loadFrozenColumnCount, loadHiddenColumns, normalizeColumnOrder, spliceColumnOrderBeside,
} from './gridColumnState';

/** Grid commands and their persisted display preferences share the existing grid handle. */
export function useWorkspaceGridController({
  project, sheet, chromeState, refreshHistory, refreshSheets, showError,
}: {
  project: ProjectInfo;
  sheet: SheetMeta | undefined;
  chromeState: Pick<ReturnType<typeof useWorkspaceChromeState>,
    'setOpenSplit' | 'setDocumentView' | 'setPromotedViews' | 'promotedViews'>;
  refreshHistory: () => Promise<unknown>;
  refreshSheets: () => Promise<unknown>;
  showError: (message: string) => void;
}) {
  const { projectApi } = useWorkspaceStores();
  const projectData = useProjectDataResource();
  const gridView = useGridViewHandle();
  const detail = useDetailHandle();
  const selection = useSelectionHandle();
  const actSurface = useActSurfaceHandle();
  const workView = useWorkViewHandle();
  const savedViews = useSavedViewsHandle();
  const views = useSelector(savedViews.store, (state) => state.views);
  const activeSavedViewId = useSelector(gridView.store, (state) => state.activeSavedViewId);
  const childFilter = useSelector(detail.store, (state) => state.childFilter);
  const headerMenu = useSelector(detail.store, (state) => state.headerMenu);
  const addColumnPrompt = useSelector(actSurface.store, (state) => state.addColumnPrompt);
  const activePromotedKey = useSelector(workView.store, (state) => state.activePromotedKey);
  const { setOpenSplit, setDocumentView, setPromotedViews, promotedViews } = chromeState;
  const closeHeaderMenu = useCallback(() => detail.closeHeaderMenu(), [detail]);
  const draftSortColumn = useSelector(gridView.store, (s) => s.draft.sortColumn);
  const draftSortDirection = useSelector(gridView.store, (s) => s.draft.sortDirection);
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const scopeRowIds = useSelector(gridView.store, (s) => s.applied.scopeRowIds ?? null);
  const rowHeight = useSelector(gridView.store, (s) => s.rowHeight);
  const wrapText = useSelector(gridView.store, (s) => s.wrapText);
  const columnGroupSpecs = useSelector(gridView.store, (s) => s.columnGroupSpecs);
  const columnOrderBySheet = useSelector(gridView.store, (s) => s.columnOrderBySheet);
  const frozenColumnCountBySheet = useSelector(gridView.store, (s) => s.frozenColumnCountBySheet);
  const hiddenColumnsBySheet = useSelector(gridView.store, (s) => s.hiddenColumnsBySheet);
  const columnGroupsVersion = useSelector(gridView.store, (s) => s.columnGroupsVersion);
  const changeRowHeight = useCallback(
    (n: number) => {
      gridView.setRowHeight(n);
      localStorage.setItem('frisket:row-height', String(n));
    },
    [gridView],
  );
  const toggleWrapText = useCallback(() => {
    const before = gridView.store.get();
    gridView.toggleWrap();
    const after = gridView.store.get();
    localStorage.setItem('frisket:wrap-text', after.wrapText ? '1' : '0');
    if (after.rowHeight !== before.rowHeight) {
      localStorage.setItem('frisket:row-height', String(after.rowHeight));
    }
  }, [gridView]);

  const effectiveSortColumn = sheet?.columns.some((column) => column.name === draftSortColumn)
    ? draftSortColumn
    : sheet?.columns[0]?.name ?? '';
  const activeColumnOrder = useMemo(
    () => (sheet
      ? normalizeColumnOrder(
          columnOrderBySheet[sheet.id] ?? loadColumnOrder(project.id, sheet),
          sheet,
        )
      : []),
    [columnOrderBySheet, project.id, sheet],
  );
  const columnOrderIsCustom = sheet ? hasCustomColumnOrder(activeColumnOrder, sheet) : false;
  const storedActiveHiddenColumns = useMemo(
    () => (sheet ? hiddenColumnsBySheet[sheet.id] ?? loadHiddenColumns(project.id, sheet) : []),
    [hiddenColumnsBySheet, project.id, sheet],
  );
  const groupManagedHiddenColumns = useMemo(
    () => groupManagedHiddenColumnNames(columnGroupSpecs),
    [columnGroupSpecs],
  );
  const activeSavedView = useMemo(
    () => activeSavedViewId === null || !sheet
      ? null
      : views.find(
          (view) => view.id === activeSavedViewId && String(view.sheet_id) === sheet.id,
        ) ?? null,
    [activeSavedViewId, sheet, views],
  );
  const activeExactViewColumns = useMemo(() => {
    const raw = activeSavedView?.spec.columns;
    return Array.isArray(raw)
      ? raw.filter((name): name is string => typeof name === 'string')
      : null;
  }, [activeSavedView]);
  const activeExactHiddenColumns = useMemo(() => {
    if (!sheet || activeExactViewColumns === null) return [];
    return exactViewHiddenColumns(sheet, activeExactViewColumns, groupManagedHiddenColumns);
  }, [activeExactViewColumns, groupManagedHiddenColumns, sheet]);
  const activeHiddenColumns = useMemo(
    () => [...new Set([...storedActiveHiddenColumns, ...activeExactHiddenColumns])],
    [activeExactHiddenColumns, storedActiveHiddenColumns],
  );

  useEffect(() => {
    if (!sheet || !activeSavedView || activeExactViewColumns === null) return;
    if (JSON.stringify(storedActiveHiddenColumns) === JSON.stringify(activeExactHiddenColumns)) return;
    if (gridView.store.get().activeSavedViewId !== activeSavedView.id) return;
    gridView.reconcileSavedViewHiddenColumns(
      activeSavedView.id,
      sheet.id,
      activeExactHiddenColumns,
    );
    if (gridView.store.get().activeSavedViewId === activeSavedView.id) {
      localStorage.setItem(
        hiddenColumnsKey(project.id, sheet.id),
        JSON.stringify(activeExactHiddenColumns),
      );
    }
  }, [
    activeExactHiddenColumns,
    activeExactViewColumns,
    activeSavedView,
    gridView,
    project.id,
    sheet,
    storedActiveHiddenColumns,
  ]);

  const activeDisplayHiddenColumns = useMemo(() => {
    const hidden = new Set(activeHiddenColumns);
    for (const name of groupManagedHiddenColumns) hidden.add(name);
    return [...hidden];
  }, [activeHiddenColumns, groupManagedHiddenColumns]);
  const currentViewColumns = useMemo(() => {
    if (!sheet) return null;
    const defaultOrder = sheet.columns.map((column) => column.name);
    const visible = activeColumnOrder.filter((name) => !activeDisplayHiddenColumns.includes(name));
    return columnOrderIsCustom || activeDisplayHiddenColumns.length > 0 ||
      JSON.stringify(visible) !== JSON.stringify(defaultOrder)
      ? visible
      : null;
  }, [activeColumnOrder, activeDisplayHiddenColumns, columnOrderIsCustom, sheet]);
  const headerMenuColumnStillVisible =
    !!headerMenu && !!sheet?.columns.some((column) => column.id === headerMenu.column.id);
  const visibleHeaderMenu = headerMenuColumnStillVisible ? headerMenu : null;
  const activeChildFilter =
    childFilter && sheet && childFilter.sheetId === sheet.id ? childFilter : null;
  const applyGridFilterForColumn = useCallback(
    (
      columnName: string,
      operatorIn: GridFilterOperator,
      rawValue: string,
      rawStart = '',
      rawEnd = '',
    ) => {
      if (!sheet || !columnName) return;
      const selectedColumn = sheet.columns.find((column) => column.name === columnName);
      const operator = filterOperatorForColumnType(selectedColumn?.type, operatorIn);
      const value = rawValue.trim();
      const start = rawStart.trim();
      const end = rawEnd.trim();
      const next: GridFilterSpec = {
        [columnName]: filterConditionFromDraft(operator, value, start, end),
      };

      applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
        filter: next,
      });

    },
    [detail, gridView, selection, sheet],
  );

  const applyGridBboxFilter = useCallback(
    (columnName: string, bbox: [number, number, number, number]) => {
      if (!sheet || !columnName) return;
      const value: GridFilterBboxValue = {
        min_lon: bbox[0],
        min_lat: bbox[1],
        max_lon: bbox[2],
        max_lat: bbox[3],
      };

      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { bbox: value } },
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridFilterSpec = useCallback(
    (filter: GridFilterSpec | null) => {
      if (!sheet) return;
      const [columnName, condition] = Object.entries(filter ?? {})[0] ?? [];
      if (!columnName || !condition) {
        clearGridFilterTransition({ gridView, selection }, sheet.id);
        return;
      }
      const previous = gridView.store.get().applied;
      const preservesEntityLabel = previous.filterValueLabel !== null
        && Object.entries(previous.filter ?? {}).some(([column, previousCondition]) => (
          previousCondition.entity_eq !== undefined
          && JSON.stringify(filter?.[column]?.entity_eq) === JSON.stringify(previousCondition.entity_eq)
        ));
      applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
        filter: filter ?? {},
        filterValueLabel: preservesEntityLabel ? previous.filterValueLabel : null,
      });
    },
    [detail, gridView, selection, sheet],
  );

  const applyGridFailedFilter = useCallback(
    (columnName: string, outcome: string) => {
      if (!sheet || !columnName) return;
      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { failed: outcome } },
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridEntityFilter = useCallback(
    (columnName: string, value: GridFilterEntityValue, valueLabel?: string) => {
      if (!sheet || !columnName) return;
      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { entity_eq: value } },
          filterValueLabel: valueLabel ?? null,
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridSortForColumn = useCallback(
    (columnName: string, direction: GridSortDirection) => {
      if (!sheet || !columnName) return;

      applyGridSortTransition({ gridView, detail, selection }, sheet.id, {
        column: columnName,
        direction,
        sort: [{ column: columnName, dir: direction }],
      });
    },
    [detail, gridView, selection, sheet],
  );

  const sortHeaderColumn = useCallback(
    (column: ColumnDef) => {
      const current = activeGridSort?.find((rule) => rule.column === column.name);
      const direction: GridSortDirection = current?.dir === 'asc' ? 'desc' : 'asc';
      applyGridSortForColumn(column.name, direction);
    },
    [activeGridSort, applyGridSortForColumn],
  );

  const applyInlineFilter = useCallback(
    (columnName: string, rawValue: string) => {
      if (!sheet || !columnName) return;
      const value = rawValue;
      const base: GridFilterSpec = { ...(activeGridFilter ?? {}) };
      if (value.trim() === '') delete base[columnName];
      else base[columnName] = { contains: value };

      if (Object.keys(base).length === 0) {
        clearGridFilterTransition({ gridView, selection }, sheet.id);
      } else {
        applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
          filter: base,
        });
      }
    },
    [detail, gridView, selection, sheet, activeGridFilter],
  );

  const openGridHeaderMenu = useCallback(
    ({ column, columnIndex, bounds }: HeaderMenuState) => {
      detail.openHeaderMenu({ column, columnIndex, bounds });
    },
    [detail],
  );

  const applyGridSort = useCallback(() => {
    applyGridSortForColumn(effectiveSortColumn, draftSortDirection);
  }, [applyGridSortForColumn, draftSortDirection, effectiveSortColumn]);

  const clearGridFilter = useCallback(() => {

    clearGridFilterTransition({ gridView, selection }, sheet?.id);
  }, [gridView, selection, sheet]);

  const clearGridSort = useCallback(() => {
    clearGridSortTransition({ gridView, selection }, sheet?.id);
  }, [gridView, selection, sheet]);

  const headerMenuActiveSort = useMemo(() => {
    if (!headerMenu) return null;
    return activeGridSort?.find((item) => item.column === headerMenu.column.name) ?? null;
  }, [activeGridSort, headerMenu]);

  const headerMenuActiveFilter = useMemo(() => {
    if (!headerMenu) return null;
    return activeGridFilter?.[headerMenu.column.name] ?? null;
  }, [activeGridFilter, headerMenu]);

  const changeColumnOrder = useCallback(
    (columns: string[]) => {
      if (!sheet) return;
      const next = normalizeColumnOrder(columns, sheet);
      gridView.setColumnOrder(sheet.id, next);
      localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(next));
    },
    [gridView, project.id, sheet],
  );

  const hiddenColumnNames = activeDisplayHiddenColumns;

  const headerMenuAdjacentHidden = useMemo(() => {
    if (!headerMenu) return [];
    const userHidden = new Set(activeHiddenColumns);
    const index = activeColumnOrder.indexOf(headerMenu.column.name);
    if (index === -1) return [];
    const run: string[] = [];
    for (let i = index - 1; i >= 0 && userHidden.has(activeColumnOrder[i]); i -= 1) {
      run.unshift(activeColumnOrder[i]);
    }
    for (let i = index + 1; i < activeColumnOrder.length && userHidden.has(activeColumnOrder[i]); i += 1) {
      run.push(activeColumnOrder[i]);
    }
    return run;
  }, [activeColumnOrder, activeHiddenColumns, headerMenu]);

  const visibleOrderedColumnNames = useMemo(() => {
    if (!sheet) return [];
    const hidden = new Set(hiddenColumnNames);
    return activeColumnOrder.filter((name) => !hidden.has(name));
  }, [activeColumnOrder, hiddenColumnNames, sheet]);
  const visibleOrderedColumns = useMemo(() => {
    if (!sheet) return [];
    const byName = new Map(sheet.columns.map((column) => [column.name, column]));
    return visibleOrderedColumnNames
      .map((name) => byName.get(name))
      .filter((column): column is ColumnDef => column !== undefined);
  }, [sheet, visibleOrderedColumnNames]);

  const activeFrozenColumnCount = useMemo(() => {
    if (!sheet) return 0;
    return clampFrozenColumnCount(
      frozenColumnCountBySheet[sheet.id] ??
        loadFrozenColumnCount(project.id, sheet.id, visibleOrderedColumnNames.length),
      visibleOrderedColumnNames.length,
    );
  }, [frozenColumnCountBySheet, project.id, sheet, visibleOrderedColumnNames.length]);

  const changeFrozenColumnCount = useCallback(
    (count: number) => {
      if (!sheet) return;
      const next = clampFrozenColumnCount(count, visibleOrderedColumnNames.length);
      gridView.setFrozenColumnCount(sheet.id, next);
      localStorage.setItem(frozenColumnsKey(project.id, sheet.id), String(next));
      closeHeaderMenu();
    },
    [closeHeaderMenu, gridView, project.id, sheet, visibleOrderedColumnNames.length],
  );

  const setHiddenColumns = useCallback(
    (updater: (previous: string[]) => string[]) => {
      if (!sheet) return;
      const previous = hiddenColumnsBySheet[sheet.id] ?? loadHiddenColumns(project.id, sheet);
      const known = new Set(sheet.columns.map((col) => col.name));
      const seen = new Set<string>();
      const next = updater(previous).filter(
        (name) => known.has(name) && !seen.has(name) && (seen.add(name), true),
      );
      const nextSet = new Set(next);
      const newlyShownDefaults = previous.filter(
        (name) => !nextSet.has(name) && sheet.columns.some((column) => column.name === name && column.defaultHidden),
      );
      if (newlyShownDefaults.length) {
        const key = shownDefaultColumnsKey(project.id, sheet.id);
        const stored = localStorage.getItem(key);
        const shown = new Set<string>(stored ? JSON.parse(stored) as string[] : []);
        for (const name of newlyShownDefaults) shown.add(name);
        localStorage.setItem(key, JSON.stringify([...shown]));
      }
      gridView.setHiddenColumns(sheet.id, next);
      localStorage.setItem(hiddenColumnsKey(project.id, sheet.id), JSON.stringify(next));
    },
    [gridView, hiddenColumnsBySheet, project.id, sheet],
  );

  const hideColumn = useCallback(
    (name: string) => {
      setHiddenColumns((previous) => [...previous, name]);
      closeHeaderMenu();
    },
    [closeHeaderMenu, setHiddenColumns],
  );

  const setRowTitleColumn = useCallback(
    (columnId: string) => {
      if (!sheet) return;
      closeHeaderMenu();
      void projectApi
        .setSheetTitleColumn(sheet.id, columnId)
        .then(() => refreshSheets())
        .catch((e: Error) => showError(e.message));
    },
    [sheet, projectApi, refreshSheets, showError, closeHeaderMenu],
  );

  const revealColumns = useCallback(
    (names: string[]) => {
      const drop = new Set(names);
      setHiddenColumns((previous) => previous.filter((name) => !drop.has(name)));
      closeHeaderMenu();
    },
    [closeHeaderMenu, setHiddenColumns],
  );

  const revealAllHiddenColumns = useCallback(() => {
    setHiddenColumns(() => []);
  }, [setHiddenColumns]);

  const openAddColumnPrompt = useCallback((anchor: { x: number; y: number }) => {
    actSurface.openAddColumnPromptAt(anchor);
  }, [actSurface]);

  const closeAddColumnPrompt = useCallback(
    () => actSurface.closeAddColumnPrompt(),
    [actSurface],
  );

  const insertColumnBeside = useCallback(
    (side: 'left' | 'right') => {
      if (!sheet || !headerMenu) return;
      const index = sheet.columns.findIndex((col) => col.id === headerMenu.column.id);
      const position = index === -1 ? null : index + 1 + (side === 'right' ? 1 : 0);

      insertColumnBesideTransition(
        { detail, actSurface },
        {
          position,
          anchor: {
            x: headerMenu.bounds.x,
            y: headerMenu.bounds.y + headerMenu.bounds.height,
          },
          anchorName: headerMenu.column.name,
          side,
        },
      );
    },
    [actSurface, detail, headerMenu, sheet],
  );

  const submitAddColumn = useCallback(
    async (name: string, type: string) => {
      if (!sheet) return;
      const trimmed = name.trim();
      if (!trimmed) return;
      const position = addColumnPrompt?.position ?? null;
      const anchorName = addColumnPrompt?.anchorName ?? null;
      const side = addColumnPrompt?.side ?? 'right';
      try {
        const added = await projectApi.addColumn(sheet.id, trimmed, { type, position });
        const next = anchorName
          ? spliceColumnOrderBeside(activeColumnOrder, added.name, anchorName, side)
          : [...activeColumnOrder.filter((column) => column !== added.name), added.name];
        completeAddColumnTransition({ gridView, actSurface }, sheet.id, next);
        localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(next));
        projectData.invalidate();
        await refreshSheets();
        void refreshHistory();
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [actSurface, activeColumnOrder, addColumnPrompt, gridView, project.id, projectApi, projectData, refreshHistory, refreshSheets, sheet, showError],
  );

  return useMemo(() => ({
    activeGridFilter, activeGridSort, scopeRowIds, rowHeight, wrapText, columnGroupSpecs,
    columnGroupsVersion, activeColumnOrder, activeHiddenColumns, hiddenColumnNames,
    currentViewColumns, visibleHeaderMenu, activeChildFilter, effectiveSortColumn,
    activeFrozenColumnCount, visibleOrderedColumns, headerMenuActiveSort, headerMenuActiveFilter,
    headerMenuAdjacentHidden, changeRowHeight, toggleWrapText, changeColumnOrder,
    changeFrozenColumnCount, hideColumn, revealColumns, revealAllHiddenColumns, setRowTitleColumn,
    applyGridFilterForColumn, applyGridBboxFilter, applyGridFilterSpec, applyGridFailedFilter,
    applyGridEntityFilter, applyGridSortForColumn, sortHeaderColumn, applyInlineFilter,
    applyGridSort, clearGridFilter, clearGridSort, openGridHeaderMenu, closeHeaderMenu,
    openAddColumnPrompt, closeAddColumnPrompt, insertColumnBeside, submitAddColumn,
  }), [
    activeGridFilter, activeGridSort, scopeRowIds, rowHeight, wrapText, columnGroupSpecs,
    columnGroupsVersion, activeColumnOrder, activeHiddenColumns, hiddenColumnNames,
    currentViewColumns, visibleHeaderMenu, activeChildFilter, effectiveSortColumn,
    activeFrozenColumnCount, visibleOrderedColumns, headerMenuActiveSort, headerMenuActiveFilter,
    headerMenuAdjacentHidden, changeRowHeight, toggleWrapText, changeColumnOrder,
    changeFrozenColumnCount, hideColumn, revealColumns, revealAllHiddenColumns, setRowTitleColumn,
    applyGridFilterForColumn, applyGridBboxFilter, applyGridFilterSpec, applyGridFailedFilter,
    applyGridEntityFilter, applyGridSortForColumn, sortHeaderColumn, applyInlineFilter,
    applyGridSort, clearGridFilter, clearGridSort, openGridHeaderMenu, closeHeaderMenu,
    openAddColumnPrompt, closeAddColumnPrompt, insertColumnBeside, submitAddColumn,
  ]);
}
