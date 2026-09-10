// The command palette's SEARCH section (project-wide FTS). Purely
// presentational: all state/logic stays in the parent and is threaded in as
// props — DOM/classes/testids are relied on by tests, so keep them stable.
import { Bell } from 'lucide-react';
import type { SearchHit } from '../api/open';
import { Snip } from '../components/SearchPanel';
import { PanelSelect } from '../components/PanelSelect';

export function PaletteSearchSection({
  mode,
  onModeChange,
  rerank,
  onRerankChange,
  canWatch,
  watchBusy,
  watchStatus,
  onCreateWatch,
  searchError,
  hits,
  query,
  searchGroups,
  sheetName,
  activeIndex,
  rowIndexOfHit,
  onHoverHit,
  onActivateHit,
}: {
  mode: 'keyword' | 'semantic';
  onModeChange(mode: 'keyword' | 'semantic'): void;
  rerank: boolean;
  onRerankChange(rerank: boolean): void;
  canWatch: boolean;
  watchBusy: boolean;
  watchStatus: string | null;
  onCreateWatch(): void;
  searchError: string | null;
  hits: SearchHit[] | null;
  query: string;
  searchGroups: { sheetId: string; hits: SearchHit[] }[];
  sheetName(id: string | number): string;
  activeIndex: number;
  rowIndexOfHit(hit: SearchHit): number;
  onHoverHit(index: number): void;
  onActivateHit(hit: SearchHit): void;
}) {
  return (
    <div className="palette-section palette-search-section" data-testid="palette-section-search">
      <div className="palette-section-label">
        Search
        <span className="palette-search-controls">
          <PanelSelect
            className="row-height-select"
            data-testid="search-mode-select"
            aria-label="Search mode"
            value={mode}
            onChange={(event) => onModeChange(event.target.value as 'keyword' | 'semantic')}
          >
            <option value="keyword">Keyword</option>
            <option value="semantic">Semantic</option>
          </PanelSelect>
          <label className="palette-search-rerank">
            <input
              type="checkbox"
              data-testid="search-rerank-toggle"
              checked={rerank}
              onChange={(event) => onRerankChange(event.target.checked)}
            />
            <span>Rerank</span>
          </label>
          <button
            type="button"
            className="mini-btn search-watch-btn"
            data-testid="watch-search-button"
            disabled={!canWatch}
            onClick={onCreateWatch}
            title={
              mode === 'keyword'
                ? 'Watch this search'
                : 'Watchlists MVP supports keyword searches'
            }
          >
            <Bell size={12} /> {watchBusy ? 'Watching…' : 'Watch search'}
          </button>
          {watchStatus && (
            <span className="search-watch-status" data-testid="watch-search-status">
              {watchStatus}
            </span>
          )}
        </span>
      </div>
      {searchError && <div className="picker-error">{searchError}</div>}
      {!searchError && hits !== null && (
        hits.length === 0 ? (
          <div className="search-empty">No matches for “{query.trim()}”.</div>
        ) : (
          <div className="search-results" data-testid="search-results">
            {searchGroups.map((group) => (
              <div key={group.sheetId} className="search-group">
                <div className="search-group-label">{sheetName(group.sheetId)}</div>
                {group.hits.map((hit) => {
                  const idx = rowIndexOfHit(hit);
                  return (
                    <button
                      key={`${hit.sheet_id}:${hit.row_id}:${hit.column_id}:${hit.snip}`}
                      type="button"
                      className={`search-hit${idx === activeIndex ? ' active' : ''}`}
                      data-testid="search-hit"
                      onMouseEnter={() => idx >= 0 && onHoverHit(idx)}
                      onClick={() => onActivateHit(hit)}
                    >
                      <span className="search-hit-col">{hit.column_name}</span>
                      <span className="search-hit-snip">
                        <Snip text={hit.snip} decodeEscapes={Boolean(hit.ai_generated)} />
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        )
      )}
    </div>
  );
}
