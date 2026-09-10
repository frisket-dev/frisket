import { useEffect, useReducer, useRef, useState } from 'react';
import { Bell, Search } from 'lucide-react';
import { searchProject, type SearchHit, type SheetMeta } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { decodeEscapedText } from '../displayText';
import { requestGridCellReveal } from '../grid/gridCellReveal';
import { PanelSelect } from './PanelSelect';

export interface SearchPanelProps {
  sheets: SheetMeta[];
  /** Switch the workspace to this sheet after this panel queues the matched
   *  grid-cell handoff. */
  onPick(sheetId: string, rowId: string): void;
}

type SearchState = {
  open: boolean;
  q: string;
  mode: 'keyword' | 'semantic';
  rerank: boolean;
  hits: SearchHit[] | null;
  error: string | null;
};

type SearchAction =
  | { type: 'open' }
  | { type: 'toggleOpen' }
  | { type: 'close' }
  | { type: 'queryChanged'; value: string }
  | { type: 'modeChanged'; value: 'keyword' | 'semantic' }
  | { type: 'rerankChanged'; value: boolean }
  | { type: 'searchLoaded'; hits: SearchHit[] }
  | { type: 'searchFailed'; message: string };

const SEARCH_INITIAL_STATE: SearchState = {
  open: false,
  q: '',
  mode: 'keyword',
  rerank: false,
  hits: null,
  error: null,
};

function searchReducer(state: SearchState, action: SearchAction): SearchState {
  switch (action.type) {
    case 'open':
      return { ...state, open: true };
    case 'toggleOpen':
      return { ...state, open: !state.open };
    case 'close':
      return { ...SEARCH_INITIAL_STATE, mode: state.mode, rerank: state.rerank };
    case 'queryChanged':
      return action.value.trim()
        ? { ...state, q: action.value }
        : { ...state, q: action.value, hits: null, error: null };
    case 'modeChanged':
      return { ...state, mode: action.value };
    case 'rerankChanged':
      return { ...state, rerank: action.value };
    case 'searchLoaded':
      return { ...state, hits: action.hits, error: null };
    case 'searchFailed':
      return { ...state, error: action.message };
    default:
      return state;
  }
}

/** Project-wide search: a sidebar trigger + ⌘K overlay over
 *  GET /api/projects/{pid}/search. Results grouped by sheet; snippets carry
 *  FTS <b>…</b> markers which are rendered by splitting, never as HTML. */
export function SearchPanel({ sheets, onPick }: SearchPanelProps) {
  const { projectApi, chromePreferences: { projectId } } = useWorkspaceStores();
  const [state, dispatch] = useReducer(searchReducer, SEARCH_INITIAL_STATE);
  const [watchBusy, setWatchBusy] = useState(false);
  const [watchStatus, setWatchStatus] = useState<string | null>(null);
  const { open, q, mode, rerank, hits, error } = state;
  const seq = useRef(0);

  // ⌘K / ctrl-K from anywhere in the workspace
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        dispatch({ type: 'toggleOpen' });
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // debounced query; seq guards against out-of-order responses (the empty-
  // query reset happens in the input onChange handler, not here)
  useEffect(() => {
    if (!open) return;
    const query = q.trim();
    if (!query) return;
    const mine = ++seq.current;
    const t = setTimeout(() => {
      searchProject(projectId, query, { mode, rerank })
        .then((h) => {
          if (seq.current !== mine) return;
          dispatch({ type: 'searchLoaded', hits: h });
        })
        .catch((e: Error) => {
          if (seq.current === mine) dispatch({ type: 'searchFailed', message: e.message });
        });
    }, 180);
    return () => clearTimeout(t);
  }, [q, open, mode, rerank, projectId]);

  const close = () => {
    seq.current++;
    setWatchStatus(null);
    dispatch({ type: 'close' });
  };

  const pick = (hit: SearchHit) => {
    const sheetId = String(hit.sheet_id);
    const rowId = String(hit.row_id);
    close();
    requestGridCellReveal({
      projectId,
      sheetId,
      rowId,
      columnId: String(hit.column_id),
      columnName: hit.column_name,
    });
    onPick(sheetId, rowId);
    // Grid owner may listen for this to scroll the row into view.
    window.dispatchEvent(
      new CustomEvent('frisket:reveal-row', { detail: { sheetId, rowId } }),
    );
  };

  const sheetsById = new Map(sheets.map((sheet) => [sheet.id, sheet]));
  const sheetName = (id: string) => sheetsById.get(id)?.name ?? `sheet ${id}`;
  const watchQuery = q.trim();
  const createSearchWatch = () => {
    if (!watchQuery || mode !== 'keyword' || watchBusy) return;
    setWatchBusy(true);
    setWatchStatus(null);
    void projectApi
      .createWatch({
        name: `Search: ${watchQuery}`,
        scope: { kind: 'project' },
        query: {
          kind: 'fts',
          q: watchQuery,
          mode: 'keyword',
          rerank: rerank ? 'auto' : 'off',
        },
      })
      .then(() => {
        window.dispatchEvent(new Event('frisket:watches-changed'));
        close();
      })
      .catch((e: Error) => setWatchStatus(e.message))
      .finally(() => setWatchBusy(false));
  };

  // group by sheet, preserving FTS rank order of first appearance
  const groups: { sheetId: string; hits: SearchHit[] }[] = [];
  const groupsBySheet = new Map<string, { sheetId: string; hits: SearchHit[] }>();
  for (const h of hits ?? []) {
    const sid = String(h.sheet_id);
    const group = groupsBySheet.get(sid);
    if (group) group.hits.push(h);
    else {
      const nextGroup = { sheetId: sid, hits: [h] };
      groups.push(nextGroup);
      groupsBySheet.set(sid, nextGroup);
    }
  }

  return (
    <>
      <button
        type="button"
        className="sidebar-search-btn"
        data-testid="open-search"
        onClick={() => dispatch({ type: 'open' })}
      >
        <Search size={13} />
        <span>Search</span>
        <kbd>⌘K</kbd>
      </button>

      {open && (
        <div
          className="modal-backdrop search-backdrop"
          role="presentation"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div
            className="search-pop"
            data-testid="search-panel"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.stopPropagation();
                close();
              }
            }}
          >
            <div className="search-input-row">
              <Search size={14} />
              <input
                aria-label="Search this project"
                className="search-input"
                placeholder="Search this project…"
                value={q}
                data-testid="search-input"
                onChange={(e) => {
                  if (!e.target.value.trim()) {
                    seq.current++;
                  }
                  dispatch({ type: 'queryChanged', value: e.target.value });
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && hits?.length) pick(hits[0]);
                }}
              />
              <kbd>esc</kbd>
            </div>

            <div className="search-controls">
              <label>
                <span>Mode</span>
                <PanelSelect
                  data-testid="search-mode-select"
                  value={mode}
                  onChange={(e) => {
                    dispatch({
                      type: 'modeChanged',
                      value: e.target.value as 'keyword' | 'semantic',
                    });
                  }}
                >
                  <option value="keyword">Keyword</option>
                  <option value="semantic">Semantic</option>
                </PanelSelect>
              </label>
              <label className="search-rerank">
                <input
                  type="checkbox"
                  data-testid="search-rerank-toggle"
                  checked={rerank}
                  onChange={(e) => dispatch({ type: 'rerankChanged', value: e.target.checked })}
                />
                <span>Rerank</span>
              </label>
              <button
                type="button"
                className="mini-btn search-watch-btn"
                data-testid="watch-search-button"
                disabled={!watchQuery || mode !== 'keyword' || watchBusy}
                onClick={createSearchWatch}
                title={mode === 'keyword' ? 'Watch this search' : 'Watchlists MVP supports keyword searches'}
              >
                <Bell size={12} /> {watchBusy ? 'Watching...' : 'Watch search'}
              </button>
              {watchStatus && (
                <span className="search-watch-status" data-testid="watch-search-status">
                  {watchStatus}
                </span>
              )}
            </div>

            {error && <div className="picker-error">{error}</div>}
            {!error && hits !== null && (
              hits.length === 0 ? (
                <div className="search-empty">No matches for “{q.trim()}”.</div>
              ) : (
                <div className="search-results" data-testid="search-results">
                  {groups.map((g) => (
                    <div key={g.sheetId} className="search-group">
                      <div className="search-group-label">{sheetName(g.sheetId)}</div>
                      {g.hits.map((h) => (
                        <button
                          key={`${h.sheet_id}:${h.row_id}:${h.column_id}:${h.snip}`}
                          type="button"
                          className="search-hit"
                          data-testid="search-hit"
                          onClick={() => pick(h)}
                        >
                          <span className="search-hit-col">{h.column_name}</span>
                          <span className="search-hit-snip">
                            <Snip text={h.snip} decodeEscapes={Boolean(h.ai_generated)} />
                          </span>
                        </button>
                      ))}
                    </div>
                  ))}
                </div>
              )
            )}
          </div>
        </div>
      )}
    </>
  );
}

/** Render an FTS snippet treating only <b>…</b> as markup — the text is
 *  split on the markers, never injected as HTML. Exported so the command
 *  palette's SEARCH section (the search modal's successor) renders snippets
 *  the same way. */
export function Snip({ text, decodeEscapes }: { text: string; decodeEscapes?: boolean }) {
  const parts = splitSnippet(decodeEscapes ? decodeEscapedText(text) : text);
  return (
    <>
      {parts.map((part) =>
        part.bold ? (
          <b key={part.key}>{part.text}</b>
        ) : (
          <span key={part.key}>{part.text}</span>
        ),
      )}
    </>
  );
}

function splitSnippet(text: string): Array<{ key: string; text: string; bold: boolean }> {
  const parts: Array<{ key: string; text: string; bold: boolean }> = [];
  const markers = /<\/?b>/g;
  let last = 0;
  let bold = false;
  let match: RegExpExecArray | null;
  while ((match = markers.exec(text)) !== null) {
    if (match.index > last) {
      parts.push({
        key: `${last}:${match.index}:${bold ? 'bold' : 'text'}`,
        text: text.slice(last, match.index),
        bold,
      });
    }
    bold = match[0] === '<b>';
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    parts.push({
      key: `${last}:${text.length}:${bold ? 'bold' : 'text'}`,
      text: text.slice(last),
      bold,
    });
  }
  return parts;
}
