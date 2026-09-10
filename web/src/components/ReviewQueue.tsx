import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type CSSProperties,
} from 'react';
import { Check, ChevronDown, ChevronUp, Pencil, X } from 'lucide-react';
import {
  type CellValue,
  type ReviewAction,
  type ReviewBundle,
  type ReviewBundleField,
  type ReviewBundlePage,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { confClass } from '../format';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';

export interface ReviewQueueProps {
  /** Restrict the queue to one persisted action run. The global inbox omits it. */
  runId?: string;
  onClose(): void;
  /** Called after any accept/reject/edit so the grid + badge can refresh. */
  onChanged(remaining: number): void;
}

type ReviewQueueState = {
  bundles: ReviewBundle[] | null;
  page: ReviewBundlePage | null;
  cursor: number;
  activeFieldId: string | null;
  editingFieldId: string | null;
  editValue: string;
  reviewNote: string;
  resolvingFieldId: string | null;
};

type ReviewQueueAction =
  | { type: 'loaded'; page: ReviewBundlePage }
  | { type: 'move'; delta: -1 | 1 }
  | { type: 'select-field'; fieldId: string; note: string }
  | { type: 'start-edit'; fieldId: string; value: string }
  | { type: 'edit-value'; value: string }
  | { type: 'edit-note'; note: string }
  | { type: 'cancel-edit' }
  | { type: 'start-resolve'; fieldId: string }
  | {
    type: 'resolved';
    fieldId: string;
    action: ReviewAction;
    editedValue?: CellValue;
    note?: string | null;
    includeReviewed: boolean;
  }
  | { type: 'resolve-failed' };

const REVIEW_QUEUE_INITIAL_STATE: ReviewQueueState = {
  bundles: null,
  page: null,
  cursor: 0,
  activeFieldId: null,
  editingFieldId: null,
  editValue: '',
  reviewNote: '',
  resolvingFieldId: null,
};

const REVIEW_BUNDLE_PAGE_SIZE = 25;

function clampCursor(cursor: number, itemCount: number): number {
  return Math.min(Math.max(0, cursor), Math.max(0, itemCount - 1));
}

function pendingFields(bundle: ReviewBundle): ReviewBundleField[] {
  return bundle.fields.filter((field) => field.chore);
}

function pendingFieldCount(bundles: ReviewBundle[]): number {
  return bundles.reduce((sum, bundle) => sum + pendingFields(bundle).length, 0);
}

function firstFieldId(bundle: ReviewBundle | undefined): string | null {
  if (!bundle) return null;
  return pendingFields(bundle)[0]?.id ?? bundle.fields[0]?.id ?? null;
}

function activeField(
  bundle: ReviewBundle | undefined,
  activeFieldId: string | null,
): ReviewBundleField | undefined {
  if (!bundle) return undefined;
  return (
    bundle.fields.find((field) => field.id === activeFieldId)
    ?? pendingFields(bundle)[0]
    ?? bundle.fields[0]
  );
}

function toEditString(value: CellValue): string {
  return value === null ? '' : String(value);
}

function noteForField(field: ReviewBundleField | undefined): string {
  return field?.note ?? '';
}

function reviewStateLabel(field: ReviewBundleField): string {
  switch (field.reviewDecision) {
    case 'accept': return 'accepted';
    case 'edit': return 'corrected';
    case 'reject':
    case 'reject_clear': return 'rejected';
    default: return field.reviewState;
  }
}

function actionMetaLabel(actionName: string): string {
  return `Action: ${actionName || 'unknown'}`;
}

function sameCellValue(left: CellValue, right: CellValue): boolean {
  return left === right || (left === null && right === '');
}

function nextResolvedField(
  field: ReviewBundleField,
  action: ReviewAction,
  editedValue?: CellValue,
  preserveOriginalValue = false,
): ReviewBundleField {
  if (action === 'reject' || action === 'reject_clear') {
    return {
      ...field,
      ...(action === 'reject_clear' && !preserveOriginalValue ? { value: null } : {}),
      reviewDecision: action,
      reviewState: 'rejected',
      chore: false,
    };
  }
  if (action === 'edit') {
    const value = preserveOriginalValue ? field.value : editedValue ?? null;
    return {
      ...field,
      value,
      reviewDecision: action,
      reviewState: 'verified',
      chore: false,
      changed: !sameCellValue(field.value, value),
    };
  }
  return {
    ...field,
    reviewDecision: action,
    reviewState: 'verified',
    chore: false,
  };
}

function pageWithResolvedField(
  page: ReviewBundlePage,
  fieldId: string,
  action: ReviewAction,
  editedValue?: CellValue,
  note?: string | null,
  includeReviewed = false,
): ReviewBundlePage {
  let touched = false;
  let removed = 0;
  const bundles: ReviewBundle[] = [];
  for (const bundle of page.bundles) {
    let found = false;
    const fields = bundle.fields.map((field) => {
      if (field.id !== fieldId) return field;
      found = true;
      return {
        ...nextResolvedField(field, action, editedValue, includeReviewed),
        note,
      };
    });
    if (!found) {
      bundles.push(bundle);
      continue;
    }
    touched = true;
    if (includeReviewed || fields.some((field) => field.chore)) {
      bundles.push({ ...bundle, fields });
    } else {
      removed += 1;
    }
  }
  if (!touched) return page;
  return {
    ...page,
    bundles,
    total: Math.max(0, page.total - removed),
    hasMore: removed > 0 ? bundles.length + page.offset < page.total - removed : page.hasMore,
    nextOffset: removed > 0 && bundles.length + page.offset >= page.total - removed
      ? null
      : page.nextOffset,
  };
}

function reviewQueueReducer(
  state: ReviewQueueState,
  action: ReviewQueueAction,
): ReviewQueueState {
  switch (action.type) {
    case 'loaded': {
      // A post-decision refresh can finish after the reviewer has selected a
      // sibling field and begun a note. Keep that live selection when it is
      // still present in the refreshed page instead of resetting it to the
      // first field and losing in-progress text.
      const selectedBundle = state.activeFieldId === null
        ? -1
        : action.page.bundles.findIndex((bundle) => (
          bundle.fields.some((field) => field.id === state.activeFieldId)
        ));
      const cursor = selectedBundle === -1
        ? clampCursor(state.cursor, action.page.bundles.length)
        : selectedBundle;
      const field = activeField(
        action.page.bundles[cursor],
        selectedBundle === -1 ? firstFieldId(action.page.bundles[cursor]) : state.activeFieldId,
      );
      return {
        ...state,
        bundles: action.page.bundles,
        page: action.page,
        cursor,
        activeFieldId: field?.id ?? null,
        reviewNote: selectedBundle === -1 ? noteForField(field) : state.reviewNote,
      };
    }
    case 'move': {
      const cursor = clampCursor(state.cursor + action.delta, state.bundles?.length ?? 1);
      const field = activeField(state.bundles?.[cursor], firstFieldId(state.bundles?.[cursor]));
      return {
        ...state,
        cursor,
        activeFieldId: field?.id ?? null,
        editingFieldId: null,
        editValue: '',
        reviewNote: noteForField(field),
      };
    }
    case 'select-field':
      return {
        ...state,
        activeFieldId: action.fieldId,
        editingFieldId: null,
        editValue: '',
        reviewNote: action.note,
      };
    case 'start-edit':
      return {
        ...state,
        activeFieldId: action.fieldId,
        editingFieldId: action.fieldId,
        editValue: action.value,
      };
    case 'edit-value':
      return { ...state, editValue: action.value };
    case 'edit-note':
      return { ...state, reviewNote: action.note };
    case 'cancel-edit':
      return { ...state, editingFieldId: null, editValue: '' };
    case 'start-resolve':
      return { ...state, resolvingFieldId: action.fieldId };
    case 'resolved': {
      if (state.bundles === null) return { ...state, resolvingFieldId: null };
      const currentBundleId = state.bundles[state.cursor]?.id;
      const bundles: ReviewBundle[] = [];
      for (const bundle of state.bundles) {
        const fields = bundle.fields.map((field) => (
            field.id === action.fieldId
              ? {
                ...nextResolvedField(
                  field,
                  action.action,
                  action.editedValue,
                  action.includeReviewed,
                ),
                note: action.note,
              }
              : field
        ));
        if (action.includeReviewed || fields.some((field) => field.chore)) {
          bundles.push({ ...bundle, fields });
        }
      }
      const preferredCursor = bundles.findIndex((bundle) => bundle.id === currentBundleId);
      const cursor = clampCursor(
        preferredCursor === -1 ? state.cursor : preferredCursor,
        bundles.length,
      );
      const field = activeField(bundles[cursor], firstFieldId(bundles[cursor]));
      return {
        ...state,
        bundles,
        cursor,
        activeFieldId: field?.id ?? null,
        editingFieldId: null,
        editValue: '',
        reviewNote: noteForField(field),
        resolvingFieldId: null,
      };
    }
    case 'resolve-failed':
      return { ...state, resolvingFieldId: null };
  }
}

function fieldRowStyle(selected: boolean, field: ReviewBundleField): CSSProperties {
  return {
    borderTop: '1px solid var(--bg-hover)',
    padding: '14px 0 14px 10px',
    background: field.changed ? 'var(--amber-bg)' : selected ? 'var(--accent-bg)' : 'transparent',
    boxShadow: field.changed
      ? 'inset 3px 0 0 var(--amber)'
      : selected
        ? 'inset 3px 0 0 var(--accent)'
        : 'none',
    cursor: 'pointer',
  };
}

const FIELD_SELECT_BUTTON_STYLE: CSSProperties = {
  appearance: 'none',
  border: 0,
  background: 'transparent',
  color: 'inherit',
  cursor: 'pointer',
  display: 'block',
  font: 'inherit',
  padding: 0,
  textAlign: 'left',
  width: '100%',
};

const REVIEW_SPLIT_STYLE: CSSProperties = {
  alignItems: 'stretch',
  background: 'transparent',
  border: 0,
  borderRadius: 0,
  boxShadow: 'none',
  display: 'grid',
  gap: 16,
  gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 340px), 1fr))',
  padding: 0,
  width: 'min(1080px, 100%)',
};

const REVIEW_PANEL_STYLE: CSSProperties = {
  background: 'var(--bg-panel)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  boxShadow: '0 4px 20px rgba(20, 26, 40, 0.05)',
  minWidth: 0,
  padding: '18px 20px',
};

const REVIEW_SOURCE_VALUE_STYLE: CSSProperties = {
  color: 'var(--text)',
  fontSize: 15,
  fontWeight: 650,
  lineHeight: 1.4,
  overflowWrap: 'anywhere',
  whiteSpace: 'pre-wrap',
};

const REVIEW_PANEL_TITLE_STYLE: CSSProperties = {
  fontSize: 12,
  fontWeight: 800,
  letterSpacing: 0,
  textTransform: 'uppercase',
};

function statusStyle(field: ReviewBundleField): CSSProperties {
  const color =
    field.reviewState === 'verified'
      ? 'var(--green)'
      : field.reviewState === 'rejected'
        ? 'var(--red)'
        : 'var(--text-light)';
  const background =
    field.reviewState === 'verified'
      ? 'var(--green-bg)'
      : field.reviewState === 'rejected'
        ? 'var(--red-bg)'
        : 'var(--bg-panel)';
  return {
    color,
    background,
    borderRadius: 999,
    padding: '2px 7px',
    fontSize: 11,
    fontWeight: 700,
    textTransform: 'uppercase',
  };
}

function reviewCellText(value: CellValue): string {
  return value === null ? '∅' : String(value);
}

function sourceEntriesFor(bundle: ReviewBundle): Array<[string, CellValue]> {
  const entries = Object.entries(bundle.source) as Array<[string, CellValue]>;
  if (entries.length > 0) return entries;
  return bundle.context ? [['context', bundle.context]] : [];
}

function ReviewFieldValue({ field }: { field: ReviewBundleField }) {
  if (field.columnType === 'image' && field.value) {
    return <img src={String(field.value)} alt={`${field.columnName} proposed value`} />;
  }
  return <span>{reviewCellText(field.value)}</span>;
}

interface ReviewHeaderProps {
  bundlesLoaded: boolean;
  includeReviewed: boolean;
  pendingCount: number;
  page: ReviewBundlePage | null;
  pageStart: number;
  pageEnd: number;
  runId?: string;
  onClose(): void;
  onSetIncludeReviewed(includeReviewed: boolean): void;
}

function ReviewHeader({
  bundlesLoaded,
  includeReviewed,
  pendingCount,
  page,
  pageStart,
  pageEnd,
  runId,
  onClose,
  onSetIncludeReviewed,
}: ReviewHeaderProps) {
  return (
    <header className="review-header">
      <div className="review-title">
        {runId ? `Review run #${runId}` : 'Review queue'}
        {bundlesLoaded && page && (
          <>
            <span className="muted">
              {' '}
              · {pendingCount.toLocaleString()} pending on page · sorted by confidence ↑
            </span>
            <span className="muted" data-testid="review-page-status">
              {' '}
              · Showing {pageStart.toLocaleString()}-{pageEnd.toLocaleString()} of{' '}
              {page.total.toLocaleString()} bundles
            </span>
          </>
        )}
      </div>
      <div className="review-keys">
        <kbd>a</kbd> accept <kbd>r</kbd> reject <kbd>Shift</kbd>+<kbd>r</kbd> reject and clear <kbd>e</kbd> edit <kbd>j</kbd>/<kbd>k</kbd> navigate <kbd>esc</kbd> close
      </div>
      {runId && (
        <label className="review-show-reviewed">
          <input
            type="checkbox"
            checked={includeReviewed}
            onChange={(event) => onSetIncludeReviewed(event.currentTarget.checked)}
          />
          Show reviewed
        </label>
      )}
      <button type="button" className="icon-btn" onClick={onClose} aria-label="Close review queue">
        <X size={16} />
      </button>
    </header>
  );
}

interface ReviewPageControlsProps {
  page: ReviewBundlePage | null;
  resolving: boolean;
  onNewer(): void;
  onOlder(): void;
}

function ReviewPageControls({
  page,
  resolving,
  onNewer,
  onOlder,
}: ReviewPageControlsProps) {
  return (
    <span className="review-nav">
      <button
        type="button"
        className="btn"
        data-testid="review-page-newer"
        onClick={onNewer}
        disabled={!page || page.offset <= 0 || resolving}
      >
        <ChevronUp size={15} /> Newer
      </button>
      <button
        type="button"
        className="btn"
        data-testid="review-page-older"
        onClick={onOlder}
        disabled={!page || !page.hasMore || resolving}
      >
        Older <ChevronDown size={15} />
      </button>
    </span>
  );
}

/**
 * Focused review overlay. It reviews one row/run bundle at a time while
 * resolving each sibling output field independently.
 */
interface ReviewQueueController {
  bundle: ReviewBundle | undefined;
  bundles: ReviewBundle[] | null;
  cursor: number;
  dispatch: Dispatch<ReviewQueueAction>;
  editValue: string;
  editingFieldId: string | null;
  includeReviewed: boolean;
  field: ReviewBundleField | undefined;
  focusEditInput(node: HTMLInputElement | null): void;
  loadNewerPage(): void;
  loadOlderPage(): void;
  page: ReviewBundlePage | null;
  pageEnd: number;
  pageStart: number;
  pendingCount: number;
  reviewNote: string;
  resolving: boolean;
  resolvingFieldId: string | null;
  resolve(
    target: ReviewBundleField | undefined,
    action: ReviewAction,
    edited?: CellValue,
  ): void;
  setIncludeReviewed(includeReviewed: boolean): void;
  sourceEntries: Array<[string, CellValue]>;
}

function useReviewQueueController({
  onChanged,
  onClose,
  runId,
}: ReviewQueueProps): ReviewQueueController {
  const { projectApi: api } = useWorkspaceStores();
  const [state, dispatch] = useReducer(reviewQueueReducer, REVIEW_QUEUE_INITIAL_STATE);
  const [includeReviewed, setIncludeReviewed] = useState(false);
  const pageRequestRef = useRef(0);
  const resolvingRef = useRef(false);
  const {
    bundles,
    page,
    cursor,
    activeFieldId,
    editingFieldId,
    editValue,
    reviewNote,
    resolvingFieldId,
  } = state;

  const loadPage = useCallback((offset: number) => {
    const requestId = pageRequestRef.current + 1;
    pageRequestRef.current = requestId;
    void api
      .getReviewBundles(offset, REVIEW_BUNDLE_PAGE_SIZE, runId, includeReviewed)
      .then((loadedPage) => {
        if (pageRequestRef.current !== requestId) return;
        dispatch({ type: 'loaded', page: loadedPage });
      });
  }, [includeReviewed, runId]);

  useEffect(() => {
    loadPage(0);
  }, [loadPage]);

  const loadNewerPage = useCallback(() => {
    if (!page || page.offset <= 0) return;
    loadPage(Math.max(0, page.offset - page.limit));
  }, [loadPage, page]);

  const loadOlderPage = useCallback(() => {
    if (!page || page.nextOffset === null) return;
    loadPage(page.nextOffset);
  }, [loadPage, page]);

  const bundle = bundles?.[Math.min(cursor, (bundles?.length ?? 1) - 1)];
  const field = activeField(bundle, activeFieldId);
  const pendingCount = bundles ? pendingFieldCount(bundles) : 0;
  const resolving = resolvingFieldId !== null;
  const pageStart = page && page.bundles.length > 0 ? page.offset + 1 : 0;
  const pageEnd = page ? page.offset + page.bundles.length : 0;
  const sourceEntries = bundle ? sourceEntriesFor(bundle) : [];

  const focusEditInput = useCallback((node: HTMLInputElement | null) => {
    node?.focus();
  }, []);

  const resolve = useCallback(
    (target: ReviewBundleField | undefined, action: ReviewAction, edited?: CellValue) => {
      if (
        resolvingRef.current || resolving || !bundles || !target ||
        (!target.chore && !includeReviewed)
      ) return;
      const pageOffset = page?.offset ?? 0;
      const note = reviewNote.trim() || null;
      resolvingRef.current = true;
      dispatch({ type: 'start-resolve', fieldId: target.id });
      void api.reviewItem(target.id, action, edited, note).then(
        () => {
          resolvingRef.current = false;
          dispatch({
            type: 'resolved',
            fieldId: target.id,
            action,
            ...(action === 'edit' ? { editedValue: edited ?? null } : {}),
            note,
            includeReviewed,
          });
          void (async () => {
            const requestId = pageRequestRef.current + 1;
            pageRequestRef.current = requestId;
            const [remaining, loadedPage] = await Promise.all([
              // The status-bar inbox remains global even when this overlay is
              // scoped to one run.
              api.getReviewCount(),
              api.getReviewBundles(pageOffset, REVIEW_BUNDLE_PAGE_SIZE, runId, includeReviewed),
            ]);
            let nextPage = loadedPage;
            if (loadedPage.bundles.length === 0 && loadedPage.total > 0 && pageOffset > 0) {
              const lastOffset = Math.floor((loadedPage.total - 1) / loadedPage.limit) * loadedPage.limit;
              nextPage = await api.getReviewBundles(
                lastOffset,
                REVIEW_BUNDLE_PAGE_SIZE,
                runId,
                includeReviewed,
              );
            }
            if (pageRequestRef.current !== requestId) return;
            dispatch({
              type: 'loaded',
              page: pageWithResolvedField(
                nextPage,
                target.id,
                action,
                action === 'edit' ? edited ?? null : undefined,
                note,
                includeReviewed,
              ),
            });
            onChanged(remaining);
          })();
        },
        () => {
          resolvingRef.current = false;
          dispatch({ type: 'resolve-failed' });
        },
      );
    },
    [bundles, includeReviewed, onChanged, page?.offset, resolving, reviewNote, runId],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (editingFieldId) {
        if (e.key === 'Escape') dispatch({ type: 'cancel-edit' });
        return;
      }
      const typingTarget = e.target as HTMLElement | null;
      if (typingTarget?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (resolving || resolvingRef.current) return;
      switch (e.key.toLowerCase()) {
        case 'a': resolve(field, 'accept'); break;
        case 'r': resolve(field, e.shiftKey ? 'reject_clear' : 'reject'); break;
        case 'e':
          if (field && (field.chore || includeReviewed)) {
            dispatch({ type: 'start-edit', fieldId: field.id, value: toEditString(field.value) });
          }
          e.preventDefault();
          break;
        case 'j': dispatch({ type: 'move', delta: 1 }); break;
        case 'k': dispatch({ type: 'move', delta: -1 }); break;
        // Escape is owned by useEscapeDismiss below (typing-guarded so it defers
        // to the editingFieldId cancel-edit branch above while a field is open).
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editingFieldId, field, includeReviewed, resolve, resolving]);

  useEscapeDismiss(onClose, { typingGuard: true });

  return {
    bundle,
    bundles,
    cursor,
    dispatch,
    editValue,
    editingFieldId,
    includeReviewed,
    field,
    focusEditInput,
    loadNewerPage,
    loadOlderPage,
    page,
    pageEnd,
    pageStart,
    pendingCount,
    reviewNote,
    resolving,
    resolvingFieldId,
    resolve,
    setIncludeReviewed,
    sourceEntries,
  };
}

function ReviewQueueBody({ controller }: { controller: ReviewQueueController }) {
  const { bundle, bundles } = controller;
  if (bundles === null) return <div className="review-empty">Loading queue…</div>;
  if (bundles.length === 0 || !bundle) {
    return (
      <div className="review-empty" data-testid="review-empty">
        <Check size={28} strokeWidth={1.5} />
        <p>Queue clear. Every low-confidence result has been reviewed.</p>
      </div>
    );
  }
  return <ReviewStage controller={controller} />;
}

function ReviewStage({ controller }: { controller: ReviewQueueController }) {
  const { bundle, bundles, cursor } = controller;
  if (!bundle || !bundles) return null;
  return (
    <div className="review-stage">
      <div className="review-position muted">
        {cursor + 1} of {bundles.length} bundles · {bundle.sheetName} · row {bundle.rowIndex + 1}
      </div>
      <div className="review-card" data-testid="review-card" style={REVIEW_SPLIT_STYLE}>
        <ReviewSourcePanel bundle={bundle} sourceEntries={controller.sourceEntries} />
        <ReviewOutputPanel bundle={bundle} controller={controller} />
      </div>
    </div>
  );
}

function ReviewSourcePanel({
  bundle,
  sourceEntries,
}: {
  bundle: ReviewBundle;
  sourceEntries: Array<[string, CellValue]>;
}) {
  return (
    <section
      data-testid="review-source-panel"
      aria-label="Source data and evidence"
      style={REVIEW_PANEL_STYLE}
    >
      <div
        className="review-context"
        data-testid="review-bundle-context"
        style={{ borderBottom: 0, marginBottom: 0, paddingBottom: 0 }}
      >
        <div style={REVIEW_PANEL_TITLE_STYLE}>Source data</div>
        <div className="muted" style={{ marginTop: 4 }}>
          Action input fields for this row
        </div>
        <div data-testid="review-source-fields" style={{ display: 'grid', gap: 10, marginTop: 12 }}>
          {sourceEntries.length > 0 ? (
            sourceEntries.map(([name, value]) => (
              <div
                key={name}
                data-testid={`review-source-field-${name}`}
                style={{
                  borderTop: '1px solid var(--bg-hover)',
                  display: 'grid',
                  gap: 4,
                  paddingTop: 10,
                }}
              >
                <span className="muted" style={{ fontSize: 12, fontWeight: 700 }}>{name}</span>
                <span style={REVIEW_SOURCE_VALUE_STYLE}>{reviewCellText(value)}</span>
              </div>
            ))
          ) : (
            <span className="muted">No source fields in this bundle.</span>
          )}
        </div>
      </div>
      {bundle.evidence.length > 0 && <ReviewEvidenceFields bundle={bundle} />}
    </section>
  );
}

function ReviewEvidenceFields({ bundle }: { bundle: ReviewBundle }) {
  return (
    <details
      data-testid="review-evidence"
      open
      style={{
        borderTop: '1px solid var(--bg-hover)',
        marginTop: 16,
        paddingTop: 12,
      }}
    >
      <summary className="muted">Evidence fields from the same run</summary>
      <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>
        {bundle.evidence.map((evidence) => (
          <div key={evidence.id} style={{ display: 'grid', gap: 3 }}>
            <strong>{evidence.columnName}</strong>
            <span className="muted" style={{ overflowWrap: 'anywhere' }}>
              {reviewCellText(evidence.value)}
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

function ReviewOutputPanel({
  bundle,
  controller,
}: {
  bundle: ReviewBundle;
  controller: ReviewQueueController;
}) {
  return (
    <section
      data-testid="review-output-panel"
      aria-label="Proposed output fields and review controls"
      style={{ ...REVIEW_PANEL_STYLE, display: 'flex', flexDirection: 'column' }}
    >
      <div style={REVIEW_PANEL_TITLE_STYLE}>Proposed output</div>
      <ReviewFieldsList bundle={bundle} controller={controller} />
      <div className="review-meta" style={{ flexWrap: 'wrap' }}>
        <span className={`conf-pill ${confClass(bundle.confidence)}`}>
          bundle min confidence {bundle.confidence.toFixed(2)}
        </span>
        <span className="muted">{actionMetaLabel(bundle.actionName)} · {bundle.model}</span>
      </div>
      <ReviewActionBar controller={controller} />
    </section>
  );
}

function ReviewFieldsList({
  bundle,
  controller,
}: {
  bundle: ReviewBundle;
  controller: ReviewQueueController;
}) {
  return (
    <div
      data-testid="review-bundle-fields"
      aria-label="Review fields in this row"
      style={{ marginTop: 2 }}
    >
      {bundle.fields.map((candidate) => (
        <ReviewFieldCard
          key={candidate.id}
          candidate={candidate}
          controller={controller}
          editing={controller.editingFieldId === candidate.id}
          resolving={controller.resolvingFieldId === candidate.id}
          selected={candidate.id === controller.field?.id}
        />
      ))}
    </div>
  );
}

function ReviewFieldCard({
  candidate,
  controller,
  editing,
  resolving,
  selected,
}: {
  candidate: ReviewBundleField;
  controller: ReviewQueueController;
  editing: boolean;
  resolving: boolean;
  selected: boolean;
}) {
  const { dispatch, editValue, focusEditInput, resolve } = controller;
  return (
    <section
      data-testid={`review-field-${candidate.columnName}`}
      data-review-state={candidate.reviewState}
      data-review-changed={candidate.changed ? 'true' : 'false'}
      style={fieldRowStyle(selected, candidate)}
    >
      <button
        type="button"
        style={FIELD_SELECT_BUTTON_STYLE}
        aria-pressed={selected}
        aria-label={`Select ${candidate.columnName} review field`}
        onClick={() => dispatch({
          type: 'select-field',
          fieldId: candidate.id,
          note: noteForField(candidate),
        })}
      >
        <span style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <strong>{candidate.columnName}</strong>
          <span style={statusStyle(candidate)}>{reviewStateLabel(candidate)}</span>
          {candidate.changed && (
            <span className="muted" data-testid="review-field-changed">edited</span>
          )}
          <span className={`conf-pill ${confClass(candidate.confidence)}`}>
            confidence {candidate.confidence.toFixed(2)}
          </span>
        </span>
        <span className="review-value" style={{ display: 'block', marginTop: 8 }}>
          <ReviewFieldValue field={candidate} />
        </span>
      </button>
      {editing && (
        <input
          ref={focusEditInput}
          className="form-input review-edit"
          data-testid="review-edit-input"
          aria-label={`Edit proposed ${candidate.columnName} value`}
          value={editValue}
          onChange={(e) => dispatch({ type: 'edit-value', value: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === 'Enter') resolve(candidate, 'edit', e.currentTarget.value);
          }}
        />
      )}
      {candidate.justification && <p className="prov-justification">{candidate.justification}</p>}
      {resolving && <p className="muted">Saving…</p>}
    </section>
  );
}

function ReviewActionBar({ controller }: { controller: ReviewQueueController }) {
  const {
    dispatch,
    field,
    includeReviewed,
    loadNewerPage,
    loadOlderPage,
    page,
    resolving,
    resolve,
    reviewNote,
  } = controller;
  const canResolve = Boolean(field && (field.chore || includeReviewed));
  return (
    <div className="review-actions" style={{ flexWrap: 'wrap', width: '100%' }}>
      <label className="review-note">
        <span>Review note <span className="muted">(optional)</span></span>
        <textarea
          className="form-input"
          data-testid="review-note-input"
          aria-label="Review note for selected result"
          value={reviewNote}
          onChange={(event) => dispatch({ type: 'edit-note', note: event.currentTarget.value })}
          rows={2}
        />
      </label>
      <button
        type="button"
        className="btn btn-accept"
        data-testid="review-accept"
        onClick={() => resolve(field, 'accept')}
        disabled={resolving || !canResolve}
      >
        <Check size={14} /> Accept <kbd>a</kbd>
      </button>
      <button
        type="button"
        className="btn btn-reject"
        data-testid="review-reject"
        onClick={() => resolve(field, 'reject')}
        disabled={resolving || !canResolve}
      >
        <X size={14} /> Reject <kbd>r</kbd>
      </button>
      <button
        type="button"
        className="btn btn-reject"
        data-testid="review-reject-clear"
        aria-label="Reject and clear selected result"
        title="Reject and clear (Shift+R)"
        onClick={() => resolve(field, 'reject_clear')}
        disabled={resolving || !canResolve}
      >
        <X size={14} /> Reject and clear <kbd>Shift</kbd>+<kbd>r</kbd>
      </button>
      <button
        type="button"
        className="btn"
        data-testid="review-edit"
        disabled={resolving || !canResolve}
        onClick={() => {
          if (field) {
            dispatch({ type: 'start-edit', fieldId: field.id, value: toEditString(field.value) });
          }
        }}
      >
        <Pencil size={13} /> Edit <kbd>e</kbd>
      </button>
      <span className="muted">{field ? `Selected: ${field.columnName}` : 'No field selected'}</span>
      <ReviewPageControls
        page={page}
        resolving={resolving}
        onNewer={loadNewerPage}
        onOlder={loadOlderPage}
      />
      <span className="review-nav">
        <button
          type="button"
          className="icon-btn"
          onClick={() => dispatch({ type: 'move', delta: -1 })}
          aria-label="Previous"
        >
          <ChevronUp size={15} />
        </button>
        <button
          type="button"
          className="icon-btn"
          onClick={() => dispatch({ type: 'move', delta: 1 })}
          aria-label="Next"
        >
          <ChevronDown size={15} />
        </button>
      </span>
    </div>
  );
}

export function ReviewQueue({ onClose, onChanged, runId }: ReviewQueueProps) {
  const controller = useReviewQueueController({ onClose, onChanged, runId });
  return (
    <dialog
      className="review-overlay"
      data-testid="review-queue"
      aria-label="Review queue"
      open
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <ReviewHeader
        bundlesLoaded={controller.bundles !== null}
        includeReviewed={controller.includeReviewed}
        pendingCount={controller.pendingCount}
        page={controller.page}
        pageStart={controller.pageStart}
        pageEnd={controller.pageEnd}
        runId={runId}
        onClose={onClose}
        onSetIncludeReviewed={controller.setIncludeReviewed}
      />
      <ReviewQueueBody controller={controller} />
    </dialog>
  );
}
