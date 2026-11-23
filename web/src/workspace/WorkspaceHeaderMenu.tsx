import './gridMenus.css';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { PdfTablesPicker } from '../components/PdfTablesPicker';
import { resolveTitleColumn } from '../workbench/rowTitle';
import { actionsForColumn, orderColumnActionsForNextStep } from '../actions/model';
import { pdfTablesMaterializeRequest } from '../actions/pdfTablesMaterialize';
import { columnTablesExportRequest } from '../actions/columnTablesExport';
import { isRunActionBlockedStatus } from '../runStatusModel';
import type { ActionColumnMenuEntry } from '../grid/actionRunEntry';
import { GridColumnHeaderMenu } from './gridMenus';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useSelector } from '../bind/useSelector';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { facetCondition, isFacetColumn, searchOf, withColumnContains, withColumnFacet } from './rowFiltering';
import { useMainViewModel, useRun } from './WorkspaceContext';

export function WorkspaceHeaderMenuSlot() {
  const {
    actActionTemplates,
    activeFrozenColumnCount,
    applyGridSortForColumn,
    changeFrozenColumnCount,
    applyGridFilterSpec,
    applyRowSearch,
    clearGridSort,
    closeHeaderMenu,
    headerMenuActiveFilter,
    headerMenuActiveSort,
    headerMenuAdjacentHidden,
    hideColumn,
    insertColumnBeside,
    mapAvailabilityReason,
    mapAvailabilityStatus,
    mapAvailable,
    canCreateView,
    openHeaderMenuColumnSettings,
    openHeaderMenuSaveView,
    openMapPanel,
    revealColumns,
    runActionFromSurface,
    setRowTitleColumn,
    sheet,
    startRun,
    visibleHeaderMenu,
    visibleOrderedColumns,
    loadActionRunEntry,
    compareActionRun,
    facetEligibility,
  } = useMainViewModel();
  const run = useRun();
  const gridView = useGridViewHandle();
  const activeFilter = useSelector(gridView.store, (state) => state.applied.filter);
  const selection = useCurrentViewSelection(sheet);
  const actionCatalog = useActionCatalogHandle();
  const columnTablesExportEntry = useSelector(actionCatalog.store, (state) => (
    state.status === 'ready'
      ? state.catalog?.actions.find((entry) => entry.kind === 'export.column_tables')
      : undefined
  ));

  const resolvedTitleColumn = useMemo(
    () =>
      sheet
        ? resolveTitleColumn(sheet, { columnOrder: visibleOrderedColumns.map((c) => c.name) })
        : null,
    [sheet, visibleOrderedColumns],
  );

  const columnActions = visibleHeaderMenu
    ? orderColumnActionsForNextStep(
        actionsForColumn(actActionTemplates, visibleHeaderMenu.column),
        visibleHeaderMenu.column,
      )
    : [];

  const [loadedActionRunEntry, setActionRunEntry] = useState<ActionColumnMenuEntry | null>(null);
  const actionRunEntry = loadedActionRunEntry?.anchor.sheetId === sheet?.id
    && loadedActionRunEntry?.anchor.columnId === visibleHeaderMenu?.column.id
    && loadedActionRunEntry?.anchor.runId === visibleHeaderMenu?.column.latestRunId
    ? loadedActionRunEntry : null;
  useEffect(() => {
    let active = true;
    const runId = visibleHeaderMenu?.column.latestRunId;
    if (sheet && visibleHeaderMenu && runId) {
      const anchor = { sheetId: sheet.id, columnId: visibleHeaderMenu.column.id, runId };
      void loadActionRunEntry(anchor).then(details => {
        if (active && details?.comparisonKind) setActionRunEntry({
          anchor,
          actionTitle: details.actionTitle,
          comparisonKind: details.comparisonKind,
        });
      }).catch(() => undefined);
    }
    return () => { active = false; };
  }, [visibleHeaderMenu, sheet, loadActionRunEntry]);

  const [columnTablesPicker, setColumnTablesPicker] = useState<{ id: string; name: string } | null>(null);

  const materializeColumnTables = useCallback(
    (args: { columnName: string; includeColumns: string[]; targetName: string }) => {
      const column = columnTablesPicker;
      setColumnTablesPicker(null);
      if (!sheet || !column) return;
      startRun(pdfTablesMaterializeRequest({
        sheetId: sheet.id,
        columnId: column.id,
        ...args,
      }));
    },
    [sheet, columnTablesPicker, startRun],
  );

  const exportColumnTables = useCallback(
    (args: { columnName: string; groupBy: string; excludeColumns: string[] }) => {
      const column = columnTablesPicker;
      setColumnTablesPicker(null);
      if (!sheet || !column || !columnTablesExportEntry) return;
      startRun(columnTablesExportRequest({
        sheetId: sheet.id,
        columnId: column.id,
        groupBy: args.groupBy,
        excludeColumns: args.excludeColumns,
      }));
    },
    [columnTablesExportEntry, columnTablesPicker, sheet, startRun],
  );

  const menuColumn = visibleHeaderMenu?.column ?? null;
  const facetColumn = menuColumn ? isFacetColumn(menuColumn) && !facetEligibility.searchOnly.has(menuColumn.name) : false;
  const search = searchOf({ selection, filter: activeFilter });
  const columnCondition = menuColumn ? activeFilter?.[menuColumn.name] : undefined;
  const columnFiltered = facetCondition(columnCondition) !== null
    || typeof columnCondition?.contains === 'string';

  return (
<>
              {visibleHeaderMenu && (
                <GridColumnHeaderMenu
                  state={visibleHeaderMenu}
                  actionRunEntry={actionRunEntry}
                  onCompareActionRun={anchor => { closeHeaderMenu(); compareActionRun(anchor); }}
                  activeSort={headerMenuActiveSort}
                  activeFilter={columnFiltered ? headerMenuActiveFilter : null}
                  filterLabel={facetColumn ? 'Filter…' : `Search in ${visibleHeaderMenu.column.name}…`}
                  onSort={(direction) => {
                    applyGridSortForColumn(visibleHeaderMenu.column.name, direction);
                    closeHeaderMenu();
                  }}
                  onClearSort={() => {
                    if (headerMenuActiveSort) clearGridSort();
                    closeHeaderMenu();
                  }}
                  onFilter={() => {
                    const name = visibleHeaderMenu.column.name;
                    if (facetColumn) {
                      gridView.openSidebar('filter', { facet: name });
                    } else {
                      gridView.openSidebar('filter', { search: true, scope: name });
                      // The search moves here only when this column holds no other contains.
                      const ownContains = typeof activeFilter?.[name]?.contains === 'string';
                      if (search.text && sheet && (!ownContains || search.scope === name)) {
                        applyRowSearch({ text: search.text, scope: name }, sheet.id);
                      }
                    }
                    closeHeaderMenu();
                  }}
                  onClearFilter={() => {
                    const name = visibleHeaderMenu.column.name;
                    applyGridFilterSpec(withColumnContains(withColumnFacet(activeFilter, name, null), name, null));
                    closeHeaderMenu();
                  }}
                  columnActions={columnActions}
                  onColumnAction={(template) => {
                    runActionFromSurface(template.kind, visibleHeaderMenu.column.name);
                    closeHeaderMenu();
                  }}
                  actionsDisabled={isRunActionBlockedStatus(run?.status)}
                  frozenColumnCount={activeFrozenColumnCount}
                  onFreezeColumns={(count) => changeFrozenColumnCount(count)}
                  onUnfreezeColumns={() => changeFrozenColumnCount(0)}
                  onOpenSettings={openHeaderMenuColumnSettings}
                  onSaveView={canCreateView ? openHeaderMenuSaveView : undefined}
                  onOpenMap={() => {
                    openMapPanel(visibleHeaderMenu.column);
                    closeHeaderMenu();
                  }}
                  mapAvailable={mapAvailable}
                  mapAvailabilityStatus={mapAvailabilityStatus}
                  mapAvailabilityReason={mapAvailabilityReason}
                  onHideColumn={() => hideColumn(visibleHeaderMenu.column.name)}
                  adjacentHiddenColumns={headerMenuAdjacentHidden}
                  onUnhideColumns={() => revealColumns(headerMenuAdjacentHidden)}
                  onInsertColumnLeft={() => insertColumnBeside('left')}
                  onInsertColumnRight={() => insertColumnBeside('right')}
                  isRowTitleColumn={resolvedTitleColumn?.id === visibleHeaderMenu.column.id}
                  onSetRowTitleColumn={() => setRowTitleColumn(String(visibleHeaderMenu.column.id))}
                  onExportColumnTables={() => {
                    setColumnTablesPicker({
                      id: visibleHeaderMenu.column.id,
                      name: visibleHeaderMenu.column.name,
                    });
                    closeHeaderMenu();
                  }}
                />
              )}
              {columnTablesPicker && sheet && (
                <div className="pdf-tables-picker-overlay">
                  <PdfTablesPicker
                    sheetId={sheet.id}
                    column={columnTablesPicker}
                    defaultTargetName={columnTablesPicker.name}
                    onMaterialize={materializeColumnTables}
                    onExportTables={columnTablesExportEntry ? exportColumnTables : undefined}
                    onClose={() => setColumnTablesPicker(null)}
                  />
                </div>
              )}
  </>
  );
}
