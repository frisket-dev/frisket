import './gridMenus.css';
import { useCallback, useMemo, useState } from 'react';
import { PdfTablesPicker } from '../components/PdfTablesPicker';
import { resolveTitleColumn } from '../workbench/rowTitle';
import { actionsForColumn, orderColumnActionsForNextStep } from '../actions/model';
import { pdfTablesMaterializeRequest } from '../actions/pdfTablesMaterialize';
import { columnTablesExportRequest } from '../actions/columnTablesExport';
import { isRunActionBlockedStatus } from '../runStatusModel';
import { GridColumnHeaderMenu } from './gridMenus';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useSelector } from '../bind/useSelector';
import { useMainViewModel, useWorkspaceShell, useRun } from './WorkspaceContext';

export function WorkspaceHeaderMenuSlot() {
  const {
    actActionTemplates,
    activeFrozenColumnCount,
    applyGridSortForColumn,
    changeFrozenColumnCount,
    clearGridFilter,
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
    canCreateSavedView,
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
  } = useMainViewModel();
  const { openDiscover } = useWorkspaceShell();
  const run = useRun();
  const gridView = useGridViewHandle();
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

  return (
<>
              {visibleHeaderMenu && (
                <GridColumnHeaderMenu
                  state={visibleHeaderMenu}
                  activeSort={headerMenuActiveSort}
                  activeFilter={headerMenuActiveFilter}
                  onSort={(direction) => {
                    applyGridSortForColumn(visibleHeaderMenu.column.name, direction);
                    closeHeaderMenu();
                  }}
                  onClearSort={() => {
                    if (headerMenuActiveSort) clearGridSort();
                    closeHeaderMenu();
                  }}
                  onFilter={() => {
                    if (sheet) {
                      gridView.setInlineFilterColumn(sheet.id, visibleHeaderMenu.column.name);
                    }
                    closeHeaderMenu();
                  }}
                  onClearFilter={() => {
                    if (headerMenuActiveFilter) clearGridFilter();
                    closeHeaderMenu();
                  }}
                  onOpenFilterSidebar={() => {
                    openDiscover('Facets');
                    closeHeaderMenu();
                  }}
                  onAdvancedSort={() => {
                    gridView.setSortPanelOpen(true);
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
                  onSaveView={canCreateSavedView ? openHeaderMenuSaveView : undefined}
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
