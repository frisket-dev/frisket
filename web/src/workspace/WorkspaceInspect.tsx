import '../workbench/contributions.css';
import './WorkspaceInspect.css';
import { memo, useCallback, useState } from 'react';
import { InspectDetailColumn } from '../workbench/InspectDetailColumn';
import { ResolvedWorkbenchLayoutRegion } from '../workbench/contributions';
import { type WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import { PluginPanelHost } from '../workbench/PluginPanelHost';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useRenderCount, useWorkspaceShell, useWorkspaceRowWalk, useWorkspacePluginDetailRender, useResolvedWorkbenchLayout, useCurrentSheet, useTitleColumnOrder, resolveRegionContributions, useWorkbenchHostContext } from './WorkspaceContext';

export const WorkspaceInspectDetailRegion = memo(function WorkspaceInspectDetailRegion() {
  useRenderCount('inspectDetail');
  const {
    project,
    closeRoutePanel,
    handleCellEdit,
    openRowInDocumentView,
    runActionBackfill,
  } = useWorkspaceShell();
  const walkDetailRow = useWorkspaceRowWalk();
  const renderPluginDetailTab = useWorkspacePluginDetailRender();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const { sheet } = useCurrentSheet();
  const detail = useDetailHandle();
  const rowDrawer = useSelector(detail.store, (s) => s.rowDrawer);
  const rowDrawerPreview = useSelector(detail.store, (s) => s.rowDrawerPreview);
  const selection = useSelectionHandle();
  const selectedColumnId = useSelector(selection.store, (s) => s.selectedColumnId);
  const titleColumnOrder = useTitleColumnOrder(sheet);
  // A row fetch briefly clears rowDrawer, which unmounts the Detail column.
  // Keep field disclosure state in this resident region so walking rows does
  // not discard it; this remains session-local, not a browser preference.
  const [expandedRunResultFields, setExpandedRunResultFields] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const toggleRunResultField = useCallback((fieldKey: string) => {
    setExpandedRunResultFields((previous) => {
      const next = new Set(previous);
      if (next.has(fieldKey)) next.delete(fieldKey);
      else next.add(fieldKey);
      return next;
    });
  }, []);

  const renderRowDetailContribution = useCallback(
    (contribution: WorkbenchResolvedLayoutContribution) => {
      if (!sheet || !rowDrawer) return null;
      return renderPluginDetailTab(contribution, {
        kind: 'row',
        sheetId: sheet.id,
        rowId: String(rowDrawer.id),
      });
    },
    [renderPluginDetailTab, rowDrawer, sheet],
  );

      // Per-row retries must use the cost-gated backfill path.
  const rowDrawerId = rowDrawer?.id;
  const onRetryCell = useCallback(
    (columnName: string) =>
      rowDrawerId == null
        ? Promise.resolve()
        : runActionBackfill(columnName, [Number(rowDrawerId)]),
    [rowDrawerId, runActionBackfill],
  );

  if (!rowDrawer || !sheet) return null;

  return (
    <section
      className="workbench-region workbench-inspect-detail"
      data-testid="workbench-region-inspect"
      aria-label="Inspect detail"
    >
      <InspectDetailColumn
        projectId={project.id}
        sheet={sheet}
        row={rowDrawer}
        selectedColumnId={selectedColumnId}
        preview={rowDrawerPreview}
        onClose={closeRoutePanel}
        onEdit={handleCellEdit}
        onPrev={() => walkDetailRow(-1)}
        onNext={() => walkDetailRow(1)}
        canPrev={rowDrawer.index > 0}
        canNext={rowDrawer.index < sheet.rowCount - 1}
        detailContributions={resolveRegionContributions(resolvedWorkbenchLayout, 'rowDetail').contributions}
        renderDetailContribution={renderRowDetailContribution}
        onOpenInDocumentView={(columnId) => openRowInDocumentView(String(rowDrawer.id), columnId)}
        onRetryCell={onRetryCell}
        titleColumnOrder={titleColumnOrder}
        expandedRunResultFields={expandedRunResultFields}
        onToggleRunResultField={toggleRunResultField}
      />
    </section>
  );
});

export const WorkspaceRightInspectorRegion = memo(function WorkspaceRightInspectorRegion() {
  useRenderCount('rightInspector');
  const { rightInspectorPluginPanelDescriptors } = useWorkspaceShell();
  const workbenchHostContext = useWorkbenchHostContext();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const { sheet } = useCurrentSheet();

  return (
        <section
          className="workbench-region workbench-right-inspector"
          data-testid="workbench-region-rightInspector"
          aria-label="Workbench right inspector"
        >
          <ResolvedWorkbenchLayoutRegion
            region={resolveRegionContributions(resolvedWorkbenchLayout, 'rightInspector')}
          >

          {rightInspectorPluginPanelDescriptors.map((descriptor) => (
            <PluginPanelHost
              key={descriptor.id}
              descriptor={descriptor}
              sheet={sheet ?? null}
              hostContext={workbenchHostContext}
            />
          ))}
          </ResolvedWorkbenchLayoutRegion>
        </section>
  );
});
