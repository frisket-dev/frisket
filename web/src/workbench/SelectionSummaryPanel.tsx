import type { PluginPanelContext } from './pluginPanelContext';

export const SELECTION_SUMMARY_PANEL_COMPONENT_KEY =
  'trustedLocal.demoSelectionSummary.SelectionSummaryPanel';

export function SelectionSummaryPanel({ ctx }: { ctx: PluginPanelContext }) {
  const firstSelected = ctx.selection.selectedRowIds[0] ?? null;
  const selectedRowsLabel =
    ctx.selection.selectedCount === 1 ? '1 selected row' : `${ctx.selection.selectedCount} selected rows`;
  const visibleIds = ctx.selection.selectedRowIds.slice(0, 4);
  const overflowCount = ctx.selection.selectedRowIds.length - visibleIds.length;

  return (
    <aside
      className="plugin-selection-summary-panel"
      data-testid="plugin-selection-summary-panel"
      data-contribution-id={ctx.contributionId}
      data-plugin-panel-context-schema-version={ctx.schemaVersion}
      data-active-sheet-id={ctx.sheet.id}
      data-selected-count={String(ctx.selection.selectedCount)}
    >
      <div className="panel-header">Selection</div>
      <div className="plugin-selection-summary-body">
        <div className="plugin-selection-summary-metric">
          <span data-testid="selection-summary-selected-count">{selectedRowsLabel}</span>
          <span>{ctx.sheet.name}</span>
        </div>
        <div className="plugin-selection-summary-detail" data-testid="selection-summary-sheet">
          {ctx.sheet.rowCount.toLocaleString()} sheet rows
        </div>
        <div
          className="plugin-selection-summary-id-list"
          data-testid="selection-summary-row-ids"
          aria-label="Selected row ids"
        >
          {visibleIds.length > 0 ? visibleIds.join(', ') : 'None'}
          {overflowCount > 0 ? `, +${overflowCount}` : ''}
        </div>
        <button
          type="button"
          className="mini-btn"
          data-testid="selection-summary-open-first-row"
          disabled={!firstSelected}
          onClick={() => {
            if (firstSelected) ctx.navigation.openRow(firstSelected);
          }}
        >
          Open first selected row
        </button>
      </div>
    </aside>
  );
}
