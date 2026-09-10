// Small, presentational icon-only buttons, kept mountable in isolation -- App.tsx
// itself pulls in a large transitive dependency graph (workbench contributions,
// pdfjs, ...) unsuitable for component tests.
import { Plus, Trash2 } from 'lucide-react';

/** Sheet tab strip "Add sheet" button: icon-only, its label carried on
 *  aria-label + title (semantic, not visible text) rather than rendered text. */
export function AddSheetButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      className="workbench-mainView-add-sheet"
      data-testid="workbench-mainView-add-sheet"
      aria-label="Add sheet"
      title="Add sheet"
      onClick={onClick}
    >
      <Plus size={15} />
    </button>
  );
}

/** Toolbar selection-contextual delete button: absent (not merely disabled)
 *  with no selection, icon-only with the count carried on aria-label/title.
 *  The count is real selection state, not a fixture -- the confirm dialog
 *  (ConfirmDeleteRowsModal) states it verbatim. */
export function DeleteRowsButton({ count, onClick }: { count: number; onClick: () => void }) {
  if (count <= 0) return null;
  return (
    <button
      type="button"
      className="icon-btn danger"
      data-testid="delete-rows-button"
      aria-label="Delete selected rows"
      title={`Delete ${count} selected row(s)`}
      onClick={onClick}
    >
      <Trash2 size={15} />
    </button>
  );
}
