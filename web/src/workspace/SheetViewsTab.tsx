import { useEffect } from 'react';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useSelector } from '../bind/useSelector';
import { ViewsWorkbenchPanel } from '../workbench/contributions';
import type { ViewsPanelProps } from './ViewsPanel';
import { chipSummary, deriveRowFilterChips } from './rowFiltering';
import { sameViewSelection } from './viewDefinition';
import { useMainViewModel } from './WorkspaceContext';

/** Everything the Views panel needs, read from the sheet's single state owner.
 *  The sheet sidebar's Views tab and Discover's Views panel render the same tab. */
function useSheetViewsPanelProps(): ViewsPanelProps {
  const model = useMainViewModel();
  const gridView = useGridViewHandle();
  const applied = useSelector(gridView.store, (s) => s.applied);
  const selection = useCurrentViewSelection(model.sheet);
  const chips = deriveRowFilterChips({
    selection,
    filter: applied.filter,
    filterValueLabel: applied.filterValueLabel,
    sort: applied.sort,
    scopeRowIds: applied.scopeRowIds ?? null,
  });
  const columnCount = model.sheet?.columns.length ?? 0;
  const current = selection
    ? model.views.find((view) => view.spec && sameViewSelection(view.spec.selection, selection))
    : undefined;
  return {
    views: model.views,
    viewName: model.viewName,
    editor: model.viewsEditor,
    isSaving: model.viewsSaving,
    lastPublication: model.viewsLastPublication,
    onPublicationConsumed: model.consumeViewsPublication,
    confirmation: model.viewsConfirmation,
    confirmationError: model.viewsConfirmationError,
    canEdit: model.canCreateView,
    currentViewId: current?.id ?? null,
    allRowsCurrent: chips.length === 0,
    currentSummary: `${chipSummary(chips)} · ${columnCount.toLocaleString()} ${columnCount === 1 ? 'column' : 'columns'}`,
    columnCount,
    onApplyAllRows: model.clearViewSelection,
    onNameChange: model.setViewName,
    onStartCreating: () => model.startCreatingView(),
    onSave: model.saveCurrentView,
    onUpdate: model.renameView,
    onCancel: model.cancelViewEditor,
    onEdit: model.startEditingView,
    onApply: model.applyView,
    onStartDefinitionUpdate: model.startViewDefinitionUpdate,
    onStartDelete: model.startViewDelete,
    onConfirmDefinitionUpdate: model.replaceViewDefinition,
    onConfirmDelete: model.deleteView,
    onCancelConfirmation: model.cancelViewsConfirmation,
  };
}

export function SheetViewsTab() {
  const props = useSheetViewsPanelProps();
  const { ensureViewsLoaded } = useMainViewModel();
  useEffect(() => {
    void ensureViewsLoaded();
  }, [ensureViewsLoaded]);
  return <ViewsWorkbenchPanel {...props} />;
}
