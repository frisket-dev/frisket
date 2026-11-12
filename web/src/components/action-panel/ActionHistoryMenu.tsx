import './ActionHistoryMenu.css';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ActionHistoryItem, ActionHistoryPage } from '../../api/actionHistory';
import type { GeneratedActionDraft } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { AnchoredMenu } from '../MenuPop';

const HISTORY_PAGE_SIZE = 20;
type HistoryLoadState =
  | { status: 'loading'; offset: number }
  | { status: 'ready'; offset: number; info: ActionHistoryPage }
  | { status: 'error'; offset: number; error: string };

function submittedTime(item: ActionHistoryItem): string {
  const date = new Date(item.submitted_at);
  if (!Number.isFinite(date.getTime())) return 'Time unavailable';
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}

function statusLabel(status: string): string {
  const words = status.trim().replace(/[_-]+/g, ' ');
  return words ? `${words.charAt(0).toUpperCase()}${words.slice(1)}` : 'Unknown';
}

/** History restores editable settings; execution stays in the normal Run flow. */
export function ActionHistoryMenu({ actionId, trigger, onChoose, onClose }: {
  actionId: string;
  trigger: HTMLButtonElement;
  onChoose(draft: GeneratedActionDraft): void;
  onClose(): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const triggerRef = useMemo(() => ({ current: trigger }), [trigger]);
  const requestSequence = useRef(0);
  const [state, setState] = useState<HistoryLoadState>({ status: 'loading', offset: 0 });
  const [selection, setSelection] = useState<{ id: string; error?: string } | null>(null);

  const fetchPage = useCallback((offset: number) => {
    const sequence = ++requestSequence.current;
    void projectApi.getActionHistory(actionId, offset, HISTORY_PAGE_SIZE).then(
      (info) => {
        if (requestSequence.current === sequence) setState({ status: 'ready', offset, info });
      },
      (error: unknown) => {
        if (requestSequence.current === sequence) setState({ status: 'error', offset,
          error: error instanceof Error ? error.message : 'Could not load earlier runs.' });
      },
    );
  }, [actionId, projectApi]);

  const loadPage = (offset: number) => {
    setState({ status: 'loading', offset });
    setSelection(null);
    fetchPage(offset);
  };
  useEffect(() => {
    fetchPage(0);
    return () => { requestSequence.current += 1; };
  }, [fetchPage]);

  const choose = async (id: string) => {
    const sequence = ++requestSequence.current;
    setSelection({ id });
    try {
      const detail = await projectApi.getActionHistoryEntry(id);
      if (requestSequence.current !== sequence) return;
      if (!detail.draft) throw new Error(detail.unavailable_reason ?? 'These settings are unavailable.');
      onChoose(detail.draft);
    } catch (error) {
      if (requestSequence.current === sequence) setSelection({ id,
        error: error instanceof Error ? error.message : 'Could not load these settings.' });
    }
  };
  const info = state.status === 'ready' ? state.info : null;
  const busy = selection !== null && selection.error === undefined;
  const newerOffset = state.offset > 0 ? Math.max(0, state.offset - HISTORY_PAGE_SIZE) : null;
  const olderOffset = info && info.offset + info.items.length < info.total
    ? info.offset + info.limit : null;

  return <AnchoredMenu anchorRef={triggerRef} onClose={onClose} width={360} align="right"
    dismissOnFocusOutside className="action-history-menu" data-testid="action-history-menu"
    aria-label="Earlier runs">
    {state.status === 'loading' && <div className="menu-empty" role="status">Loading…</div>}
    {state.status === 'error' && <>
      <div className="menu-error" role="alert">{state.error}</div>
      <button type="button" role="menuitem" className="menu-item" onClick={() => loadPage(state.offset)}>Try again</button>
    </>}
    {selection?.error && <div className="menu-error" role="alert">{selection.error}</div>}
    {busy && <div className="menu-empty" role="status">Loading settings…</div>}
    {info && <>
      {info.items.length === 0 ? <div className="menu-empty">No earlier runs.</div>
        : <ul className="action-history-list" role="none">
          {info.items.map(item => <li role="none" key={item.id}>
            <button type="button" role="menuitem" className="menu-item action-history-item"
              data-testid={`action-history-choose-${item.id}`} disabled={busy}
              onClick={() => void choose(item.id)}>
              <span className="menu-item-copy">
                <span className="menu-item-label">
                  <span className="action-history-column">{item.sheet_name ?? (item.sheet_id === null ? 'Project' : 'Deleted sheet')}</span>
                  <span className="action-history-status">{statusLabel(item.status)}</span>
                </span>
                <span className="menu-item-description">{submittedTime(item)}{item.run_id !== null ? ` · Run ${item.run_id}` : ''}</span>
              </span>
            </button>
          </li>)}
        </ul>}
      {(newerOffset !== null || olderOffset !== null) && <footer className="action-history-footer">
        <span>{info.offset + (info.items.length > 0 ? 1 : 0)}–{info.offset + info.items.length} of {info.total.toLocaleString()}</span>
        <div>
          <button type="button" role="menuitem" className="menu-item" disabled={busy || newerOffset === null}
            onClick={() => newerOffset !== null && loadPage(newerOffset)}><ChevronLeft size={14} aria-hidden /> Newer</button>
          <button type="button" role="menuitem" className="menu-item" disabled={busy || olderOffset === null}
            onClick={() => olderOffset !== null && loadPage(olderOffset)}>Older <ChevronRight size={14} aria-hidden /></button>
        </div>
      </footer>}
    </>}
  </AnchoredMenu>;
}
