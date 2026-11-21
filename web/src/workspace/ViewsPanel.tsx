import { PanelSelect } from '../components/PanelSelect';
import { ViewResults } from '../components/ViewResults';
import '../components/PanelPrimitives.css';
import '../components/IconButton.css';
import { ConfirmDialog } from '../components/Modal';
import './ViewsPanel.css';
import { Button } from '../components/Button';
import { TextInput, FormField } from '../components/FormControls';
// Saved-views panel. This is intentionally presentational: persistence and
// successful-create/update transitions belong to the workspace controller.
import { useEffect, useRef, useState } from 'react';
import { Pencil, Plus, Save, Trash2, X } from 'lucide-react';
import type { View, ViewSelection } from '../api/open';
import type {
  ViewsConfirmation,
  ViewsEditor,
  ViewsPublication,
} from '../state/viewsStore';
import { summarizeView } from './summarizeView';

export interface ViewsPanelProps {
  views: View[];
  viewName: string;
  editor: ViewsEditor | null;
  isSaving?: boolean;
  lastPublication?: ViewsPublication | null;
  onPublicationConsumed?(token: number): void;
  confirmation?: ViewsConfirmation | null;
  confirmationError?: string | null;
  canEdit: boolean;
  viewModified?: boolean;
  currentSelection?: ViewSelection | null;
  onSelectionChange?(selection: ViewSelection): void;
  onClearSelection?(): void;
  activeViewId?: View['id'] | null;
  onNameChange(name: string): void;
  onStartCreating(): void;
  onSave(): void;
  onUpdate(): void;
  onCancel(): void;
  onEdit(view: View): void;
  onApply(view: View): void;
  onStartDefinitionUpdate(view: View): void;
  onStartDelete(view: View): void;
  onConfirmDefinitionUpdate(): void;
  onConfirmDelete(): void;
  onCancelConfirmation(): void;
}

export function ViewsPanel({
  views,
  viewName,
  editor,
  isSaving = false,
  lastPublication = null,
  onPublicationConsumed,
  confirmation = null,
  confirmationError = null,
  canEdit,
  activeViewId = null,
  viewModified = false,
  currentSelection = null,
  onSelectionChange,
  onClearSelection,
  onNameChange,
  onStartCreating,
  onSave,
  onUpdate,
  onCancel,
  onEdit,
  onApply,
  onStartDefinitionUpdate,
  onStartDelete,
  onConfirmDefinitionUpdate,
  onConfirmDelete,
  onCancelConfirmation,
}: ViewsPanelProps) {
  const [resultsView, setResultsView] = useState<View | null>(null);
  const [statusEvent, setStatusEvent] = useState({ token: 0, message: '' });
  const nextStatusToken = useRef(0);
  const viewRows = useRef(new Map<View['id'], HTMLDivElement>());
  const listRef = useRef<HTMLDivElement>(null);
  const confirmationTrigger = useRef<HTMLElement | null>(null);
  const confirmationWasOpen = useRef(confirmation !== null);
  const nameIsBlank = viewName.trim() === '';
  const nameErrorId = 'view-name-error';
  const publishedView = lastPublication
    ? views.find((candidate) => candidate.id === lastPublication.viewId)
    : null;
  const publicationAnnouncement = lastPublication
    ? lastPublication.action === 'created'
      ? `Saved ${publishedView?.name ?? 'view'}`
      : lastPublication.action === 'renamed'
        ? `Renamed ${publishedView?.name ?? 'view'}`
        : lastPublication.action === 'updated'
          ? `Saved changes to ${publishedView?.name ?? lastPublication.viewName}.`
          : `Deleted ${lastPublication.viewName}`
    : '';
  const announce = (message: string) => {
    setStatusEvent({ token: ++nextStatusToken.current, message });
  };

  useEffect(() => {
    if (!lastPublication) return;
    if (publicationAnnouncement) {
      setStatusEvent({
        token: ++nextStatusToken.current,
        message: publicationAnnouncement,
      });
    }
    (lastPublication.focusViewId === null
      ? listRef.current
      : viewRows.current.get(lastPublication.focusViewId) ?? listRef.current
    )?.focus();
    const timeout = window.setTimeout(() => onPublicationConsumed?.(lastPublication.token), 0);
    return () => window.clearTimeout(timeout);
  }, [lastPublication, onPublicationConsumed, publicationAnnouncement]);

  useEffect(() => {
    if (confirmation) {
      confirmationWasOpen.current = true;
      return;
    }
    if (!confirmationWasOpen.current) return;
    const trigger = confirmationTrigger.current;
    confirmationTrigger.current = null;
    confirmationWasOpen.current = false;
    if (lastPublication) return;
    (trigger?.isConnected ? trigger : listRef.current)?.focus();
  }, [confirmation, lastPublication]);

  return (
    <section className="views-panel content-stack" data-testid="views-panel" aria-label="Views" aria-busy={isSaving}>
      <div
        key={lastPublication ? `publication-${lastPublication.token}` : `local-${statusEvent.token}`}
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {publicationAnnouncement || statusEvent.message}
      </div>
      {currentSelection && onSelectionChange && <div className="form-stack">
        {onClearSelection && <Button onClick={onClearSelection}>Show all rows</Button>}
        <FormField label="Search text"><TextInput aria-label="View search text" value={currentSelection.search?.text ?? ''} placeholder="All content"
          onChange={(event) => onSelectionChange({ ...currentSelection, search: event.currentTarget.value.trim() ? { text: event.currentTarget.value } : undefined,
            order: !event.currentTarget.value.trim() ? currentSelection.order.filter((item) => !('score' in item && item.score === 'search')) : currentSelection.order })} /></FormField>
        {(currentSelection.search || currentSelection.similarity) && <FormField label="Sort results"><PanelSelect aria-label="View score order"
          value={currentSelection.order[0] && 'score' in currentSelection.order[0] ? currentSelection.order[0].score : currentSelection.order.length ? 'column' : 'rows'}
          onChange={(event) => onSelectionChange({ ...currentSelection, order: event.currentTarget.value === 'rows' ? [] : [{ score: event.currentTarget.value as 'search' | 'similarity', dir: 'desc' }] })}>
          {currentSelection.order.some((item) => 'column_id' in item) && <option value="column" disabled>Current column sort</option>}
          <option value="rows">Row order</option>{currentSelection.search && <option value="search">Search score, highest first</option>}{currentSelection.similarity && <option value="similarity">Similarity score, highest first</option>}
        </PanelSelect></FormField>}
        <FormField label="Limit results"><TextInput aria-label="View result limit" type="number" min={1} placeholder="All matching rows" value={currentSelection.limit ?? ''}
          onChange={(event) => { const limit = event.currentTarget.value === '' ? null : Number(event.currentTarget.value); if (limit === null || Number.isInteger(limit) && limit > 0) onSelectionChange({ ...currentSelection, limit }); }} /></FormField>
      </div>}
      {editor && canEdit ? (
        <div
          className="views-create"
          data-testid={editor.kind === 'create' ? 'view-create' : 'view-edit'}
          aria-busy={isSaving}
        >
          {editor.kind === 'edit' && (
            <span className="view-summary" data-testid="view-filter-summary">{summarizeView(editor.view).summary}</span>
          )}
          <TextInput
            data-testid="view-name-input"
            placeholder="View name"
            value={viewName}
            aria-label="View name"
            aria-describedby={nameIsBlank ? nameErrorId : undefined}
            disabled={isSaving}
            onChange={(event) => onNameChange(event.target.value)}
          />
          {nameIsBlank && <span id={nameErrorId}>Enter a name to continue.</span>}
          <div className="views-editor-actions">
            {editor.kind === 'edit' ? (
              <Button
                type="button"
                className="btn-primary"
                data-testid="update-view-button"
                disabled={nameIsBlank || isSaving}
                onClick={onUpdate}
              >
                <Save size={14} /> {isSaving ? 'Renaming…' : 'Rename view'}
              </Button>
            ) : (
              <Button
                type="button"
                className="btn-primary"
                data-testid="save-view-button"
                disabled={nameIsBlank || isSaving}
                onClick={onSave}
              >
                <Save size={14} /> {isSaving ? 'Saving…' : 'Save view'}
              </Button>
            )}
            <Button
              type="button"

              data-testid="view-editor-cancel"
              disabled={isSaving}
              onClick={onCancel}
            >
              <X size={14} /> Cancel
            </Button>
          </div>
        </div>
      ) : (
        canEdit && (
          <Button
            type="button"
            className="btn-primary"
            data-testid="create-view"
            disabled={isSaving}
            onClick={onStartCreating}
          >
            <Plus size={14} /> Save as new view
          </Button>
        )
      )}
      <div className="views-list" data-testid="views-list" ref={listRef} tabIndex={-1}>
        {views.length === 0 ? (
          <div className="views-empty">No views yet.</div>
        ) : (
          views.map((view) => (
            <div
              className="view-item"
              data-testid="view-item"
              key={view.id}
              ref={(node) => {
                if (node) viewRows.current.set(view.id, node);
                else viewRows.current.delete(view.id);
              }}
              tabIndex={-1}
            >
              <div className="view-main">
                <button
                  type="button"
                  className="view-apply"
                  data-testid="apply-view"
                  disabled={isSaving || !!view.unavailable_reason}
                  onClick={() => {
                    if (view.sheet_id === null) setResultsView(view);
                    else { onApply(view); announce(`Applied ${view.name}`); }
                  }}
                >
                  {view.name}
                </button>
                <span className="view-summary" data-testid="view-filter-summary">{summarizeView(view).summary}</span>
                {activeViewId === view.id && <span className="view-active">{viewModified ? 'Modified' : 'Active'}</span>}
              </div>
              {canEdit && (
                <div className="view-actions">
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="update-view-definition"
                    aria-label={`Save changes to ${view.name}`}
                    title={`Save changes to ${view.name}`}
                    disabled={isSaving || view.sheet_id === null}
                    onClick={(event) => {
                      confirmationTrigger.current = event.currentTarget;
                      onStartDefinitionUpdate(view);
                    }}
                  >
                    <Save size={13} /><span className="sr-only">Save changes</span>
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="edit-view"
                    aria-label={`Rename ${view.name}`}
                    title={`Rename ${view.name}`}
                    disabled={isSaving}
                    onClick={() => onEdit(view)}
                  >
                    <Pencil size={13} /><span className="sr-only">Rename</span>
                  </button>
                  <button
                    type="button"
                    className="icon-btn danger"
                    data-testid="delete-view"
                    aria-label={`Delete ${view.name}`}
                    title={`Delete ${view.name}`}
                    disabled={isSaving}
                    onClick={(event) => {
                      confirmationTrigger.current = event.currentTarget;
                      onStartDelete(view);
                    }}
                  >
                    <Trash2 size={13} /><span className="sr-only">Delete</span>
                  </button>
                </div>
              )}
            </div>
          ))
        )}
      </div>
      {resultsView && <ViewResults key={resultsView.id} view={resultsView} onClose={() => setResultsView(null)} />}
      {confirmation && (
        <ConfirmDialog
          key={`${confirmation.kind}:${confirmation.view.id}`}
          title={confirmation.kind === 'delete' ? `Delete “${confirmation.view.name}”?`
            : `Save changes to ${confirmation.view.name}?`}
          busy={isSaving} danger={confirmation.kind === 'delete'}
          testId={confirmation.kind === 'delete' ? 'delete-view-confirmation' : 'update-view-confirmation'}
          confirmTestId={confirmation.kind === 'delete' ? 'confirm-delete-view' : 'confirm-update-view'}
          confirmLabel={isSaving
            ? confirmation.kind === 'delete' ? 'Deleting…' : 'Saving…'
            : confirmation.kind === 'delete' ? 'Delete view' : 'Save changes'}
          onCancel={onCancelConfirmation}
          onConfirm={confirmation.kind === 'delete' ? onConfirmDelete : onConfirmDefinitionUpdate}>
          <p className="modal-body-text">
            {confirmation.kind === 'delete'
              ? 'Deleting this view cannot be undone.'
              : 'Save the current selection and display. Alerts will use these saved criteria.'}
          </p>
          {confirmationError && <div className="panel-error" role="alert">{confirmationError}</div>}
        </ConfirmDialog>
      )}
    </section>
  );
}
