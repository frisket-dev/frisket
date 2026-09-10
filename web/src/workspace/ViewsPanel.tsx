// Saved-views panel. This is intentionally presentational: persistence and
// successful-create/update transitions belong to the workspace controller.
import { useEffect, useRef, useState } from 'react';
import { Pencil, Plus, Save, Trash2, X } from 'lucide-react';
import type { SavedView } from '../api/open';
import type {
  SavedViewsConfirmation,
  SavedViewsEditor,
  SavedViewsPublication,
} from '../state/savedViewsStore';
import { summarizeSavedView } from './summarizeSavedView';

export interface ViewsPanelProps {
  views: SavedView[];
  viewName: string;
  editor: SavedViewsEditor | null;
  isSaving?: boolean;
  lastPublication?: SavedViewsPublication | null;
  onPublicationConsumed?(token: number): void;
  confirmation?: SavedViewsConfirmation | null;
  confirmationError?: string | null;
  canEdit: boolean;
  activeSavedViewId?: SavedView['id'] | null;
  onNameChange(name: string): void;
  onStartCreating(): void;
  onSave(): void;
  onUpdate(): void;
  onCancel(): void;
  onEdit(view: SavedView): void;
  onApply(view: SavedView): void;
  onStartDefinitionUpdate(view: SavedView): void;
  onStartDelete(view: SavedView): void;
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
  activeSavedViewId = null,
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
  const [statusEvent, setStatusEvent] = useState({ token: 0, message: '' });
  const nextStatusToken = useRef(0);
  const savedViewRows = useRef(new Map<SavedView['id'], HTMLDivElement>());
  const listRef = useRef<HTMLDivElement>(null);
  const confirmationTrigger = useRef<HTMLElement | null>(null);
  const confirmationWasOpen = useRef(confirmation !== null);
  const nameIsBlank = viewName.trim() === '';
  const nameErrorId = 'saved-view-name-error';
  const publishedView = lastPublication
    ? views.find((candidate) => candidate.id === lastPublication.viewId)
    : null;
  const publicationAnnouncement = lastPublication
    ? lastPublication.action === 'created'
      ? `Saved ${publishedView?.name ?? 'view'}`
      : lastPublication.action === 'renamed'
        ? `Renamed ${publishedView?.name ?? 'view'}`
        : lastPublication.action === 'updated'
          ? `Saved current display to ${publishedView?.name ?? lastPublication.viewName}.`
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
      : savedViewRows.current.get(lastPublication.focusViewId) ?? listRef.current
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
    <section className="views-panel" data-testid="views-panel" aria-label="Saved views" aria-busy={isSaving}>
      <div
        key={lastPublication ? `publication-${lastPublication.token}` : `local-${statusEvent.token}`}
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {publicationAnnouncement || statusEvent.message}
      </div>
      {editor && canEdit ? (
        <div
          className="views-create"
          data-testid={editor.kind === 'create' ? 'saved-view-create' : 'saved-view-edit'}
          aria-busy={isSaving}
        >
          {editor.kind === 'edit' && (
            <span className="saved-view-summary" data-testid="saved-view-filter-summary">{summarizeSavedView(editor.view).summary}</span>
          )}
          <input
            className="form-input"
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
              <button
                type="button"
                className="btn btn-primary"
                data-testid="update-view-button"
                disabled={nameIsBlank || isSaving}
                onClick={onUpdate}
              >
                <Save size={14} /> {isSaving ? 'Renaming…' : 'Rename view'}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary"
                data-testid="save-view-button"
                disabled={nameIsBlank || isSaving}
                onClick={onSave}
              >
                <Save size={14} /> {isSaving ? 'Saving…' : 'Save view'}
              </button>
            )}
            <button
              type="button"
              className="btn"
              data-testid="saved-view-editor-cancel"
              disabled={isSaving}
              onClick={onCancel}
            >
              <X size={14} /> Cancel
            </button>
          </div>
        </div>
      ) : (
        canEdit && (
          <button
            type="button"
            className="btn btn-primary"
            data-testid="create-saved-view"
            disabled={isSaving}
            onClick={onStartCreating}
          >
            <Plus size={14} /> Save current view
          </button>
        )
      )}
      <div className="saved-views-list" data-testid="saved-views-list" ref={listRef} tabIndex={-1}>
        {views.length === 0 ? (
          <div className="views-empty">No saved views yet.</div>
        ) : (
          views.map((view) => (
            <div
              className="saved-view-item"
              data-testid="saved-view-item"
              key={view.id}
              ref={(node) => {
                if (node) savedViewRows.current.set(view.id, node);
                else savedViewRows.current.delete(view.id);
              }}
              tabIndex={-1}
            >
              <div className="saved-view-main">
                <button
                  type="button"
                  className="saved-view-apply"
                  data-testid="apply-saved-view"
                  disabled={isSaving}
                  onClick={() => {
                    onApply(view);
                    announce(`Applied ${view.name}`);
                  }}
                >
                  {view.name}
                </button>
                <span className="saved-view-summary" data-testid="saved-view-filter-summary">{summarizeSavedView(view).summary}</span>
                {activeSavedViewId === view.id && <span className="saved-view-active">Active</span>}
              </div>
              {canEdit && (
                <div className="saved-view-actions">
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="update-saved-view-definition"
                    aria-label={`Save current display to ${view.name}`}
                    title={`Save current display to ${view.name}`}
                    disabled={isSaving}
                    onClick={(event) => {
                      confirmationTrigger.current = event.currentTarget;
                      onStartDefinitionUpdate(view);
                    }}
                  >
                    <Save size={13} /><span className="sr-only">Save current display</span>
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="edit-saved-view"
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
                    data-testid="delete-saved-view"
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
      {confirmation?.kind === 'update-definition' && (
        <SavedViewConfirmationDialog
          kind="update-definition"
          view={confirmation.view}
          busy={isSaving}
          error={confirmationError}
          onCancel={onCancelConfirmation}
          onConfirm={onConfirmDefinitionUpdate}
        />
      )}
      {confirmation?.kind === 'delete' && (
        <SavedViewConfirmationDialog
          kind="delete"
          view={confirmation.view}
          busy={isSaving}
          error={confirmationError}
          onCancel={onCancelConfirmation}
          onConfirm={onConfirmDelete}
        />
      )}
    </section>
  );
}

function SavedViewConfirmationDialog({
  busy,
  error,
  kind,
  view,
  onCancel,
  onConfirm,
}: {
  busy: boolean;
  error: string | null;
  kind: 'update-definition' | 'delete';
  view: SavedView;
  onCancel(): void;
  onConfirm(): void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const isDelete = kind === 'delete';
  const titleId = `saved-view-${kind}-title-${view.id}`;
  const descriptionId = `${titleId}-description`;
  const close = () => {
    const dialog = dialogRef.current;
    if (dialog?.open) dialog.close();
    onCancel();
  };

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="modal-card"
      data-testid={isDelete ? 'delete-saved-view-confirmation' : 'update-saved-view-confirmation'}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      aria-busy={busy}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) close();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy) onConfirm();
        }}
      >
        <div className="modal-title" id={titleId}>
          {isDelete ? `Delete “${view.name}”?` : `Save current display to ${view.name}?`}
        </div>
        <p className="modal-body-text" id={descriptionId}>
          {isDelete
            ? 'Deleting this Saved View cannot be undone.'
            : 'Replace this Saved View’s filters, sorting, displayed columns, and column groups with the current display.'}
        </p>
        {error && <div className="form-error" role="alert">{error}</div>}
        <div className="form-actions">
          <button autoFocus type="button" className="btn" disabled={busy} onClick={close}>Cancel</button>
          <button
            type="submit"
            className={`btn ${isDelete ? 'btn-reject' : 'btn-primary'}`}
            data-testid={isDelete ? 'confirm-delete-saved-view' : 'confirm-update-saved-view'}
            disabled={busy}
          >
            {busy ? (isDelete ? 'Deleting…' : 'Saving…') : (isDelete ? 'Delete Saved View' : 'Save changes')}
          </button>
        </div>
      </form>
    </dialog>
  );
}
