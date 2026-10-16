import { Bell, Bookmark, SearchCheck, X } from 'lucide-react';
import type { EmbeddingIndexSummary } from '../api/open';
import type {
  ProjectSearchGroupWire,
  ProjectSearchHitWire,
  ProjectSearchResponseWire,
} from '../api/projectSearch';
import { PanelSelect } from '../components/PanelSelect';
import { decodeEscapedText } from '../displayText';

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

function Snip({ text, decodeEscapes }: { text: string; decodeEscapes?: boolean }) {
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

function coverageMessage(coverage: ProjectSearchResponseWire['coverage'] | null): string | null {
  if (!coverage || coverage.state === 'complete') return null;
  if (coverage.state === 'preparing') return 'Preparing project search material…';
  if (coverage.state === 'partial') return 'Showing available results while more material is prepared.';
  if (coverage.state === 'failed') return 'Some project material could not be searched.';
  return coverage.channels.some((channel) => channel.setup)
    ? 'Set up semantic search to include similar material.'
    : 'No indexed results are available for this search.';
}

function hitKey(hit: ProjectSearchHitWire): string {
  return `${hit.part.part_id}:${hit.binding.binding_id}`;
}

function SearchHitRow({
  hit,
  supporting,
  activeIndex,
  rowIndex,
  onHover,
  onActivate,
  onFindSimilar,
}: {
  hit: ProjectSearchHitWire;
  supporting: boolean;
  activeIndex: number;
  rowIndex: number;
  onHover(index: number): void;
  onActivate(hit: ProjectSearchHitWire): void;
  onFindSimilar(hit: ProjectSearchHitWire): void;
}) {
  return (
    <div
      className={`search-hit-row${supporting ? ' supporting' : ''}`}
      data-testid={supporting ? 'search-supporting-hit' : 'search-hit'}
      onMouseEnter={() => rowIndex >= 0 && onHover(rowIndex)}
    >
      <button
        type="button"
        className={`search-hit${rowIndex === activeIndex ? ' active' : ''}`}
        onClick={() => onActivate(hit)}
      >
        <span className="search-hit-col">{hit.column_name}</span>
        <span className="search-hit-snip">
          <Snip text={hit.snippet} decodeEscapes={hit.ai_generated} />
        </span>
      </button>
      <button
        type="button"
        className="icon-btn search-hit-similar"
        data-testid="search-hit-find-similar"
        aria-label="Find similar"
        title="Find similar"
        onClick={() => onFindSimilar(hit)}
      >
        <SearchCheck size={13} />
      </button>
    </div>
  );
}

export function PaletteSearchSection({
  mode,
  onModeChange,
  rerank,
  onRerankChange,
  namedIndexes,
  namedIndexId,
  onNamedIndexChange,
  namedIndexError,
  canWatch,
  watchBusy,
  watchStatus,
  onCreateWatch,
  canSaveLens,
  saveBusy,
  saveStatus,
  onSaveLens,
  searchError,
  groups,
  coverage,
  query,
  sheetName,
  anchorLabel,
  anchored,
  onClearAnchor,
  activeIndex,
  rowIndexOfHit,
  onHoverHit,
  onActivateHit,
  onFindSimilar,
  setupBusy,
  setupError,
  onStartSetup,
}: {
  mode: 'lexical' | 'semantic' | 'hybrid';
  onModeChange(mode: 'lexical' | 'semantic' | 'hybrid'): void;
  rerank: boolean;
  onRerankChange(rerank: boolean): void;
  namedIndexes: EmbeddingIndexSummary[];
  namedIndexId: string | undefined;
  onNamedIndexChange(indexId: string | undefined): void;
  namedIndexError: string | null;
  canWatch: boolean;
  watchBusy: boolean;
  watchStatus: string | null;
  onCreateWatch(): void;
  canSaveLens: boolean;
  saveBusy: boolean;
  saveStatus: string | null;
  onSaveLens(): void;
  searchError: string | null;
  groups: ProjectSearchGroupWire[] | null;
  coverage: ProjectSearchResponseWire['coverage'] | null;
  query: string;
  sheetName(id: string | number): string;
  anchorLabel: string | null;
  anchored: boolean;
  onClearAnchor(): void;
  activeIndex: number;
  rowIndexOfHit(hit: ProjectSearchHitWire): number;
  onHoverHit(index: number): void;
  onActivateHit(hit: ProjectSearchHitWire): void;
  onFindSimilar(hit: ProjectSearchHitWire): void;
  setupBusy: boolean;
  setupError: string | null;
  onStartSetup(setupRef: string, scope: 'workspace' | 'organization'): void;
}) {
  const status = coverageMessage(coverage);
  const setup = coverage?.channels.find((channel) => channel.setup)?.setup;
  const resultCount = groups?.length ?? 0;

  return (
    <div className="palette-section palette-search-section" data-testid="palette-section-search">
      <div className="palette-section-label">
        Search
        <span className="palette-search-controls">
          <PanelSelect
            className="row-height-select palette-search-index-select"
            data-testid="search-index-select"
            aria-label="Search collection"
            value={namedIndexId ?? ''}
            onChange={(event) => onNamedIndexChange(event.target.value || undefined)}
          >
            <option value="">Project</option>
            {namedIndexes.map((index) => (
              <option key={index.indexId} value={index.indexId}>{index.name}</option>
            ))}
          </PanelSelect>
          <PanelSelect
            className="row-height-select"
            data-testid="search-mode-select"
            aria-label="Search mode"
            value={anchored ? 'semantic' : mode}
            disabled={anchored}
            onChange={(event) =>
              onModeChange(event.target.value as 'lexical' | 'semantic' | 'hybrid')}
          >
            <option value="lexical" disabled={Boolean(namedIndexId)}>Keyword</option>
            <option value="hybrid">Hybrid</option>
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
            title={anchored && namedIndexId
              ? 'Watch rows similar to this anchor'
              : mode === 'lexical'
                ? 'Watch this search'
                : 'Watch a keyword search'}
          >
            <Bell size={12} /> {watchBusy ? 'Watching…' : 'Watch search'}
          </button>
          <button
            type="button"
            className="mini-btn search-save-btn"
            data-testid="save-search-lens-button"
            disabled={!canSaveLens}
            onClick={onSaveLens}
            title="Save this named-index search as a view"
          >
            <Bookmark size={12} /> {saveBusy ? 'Saving…' : 'Save view'}
          </button>
          {watchStatus && (
            <span className="search-watch-status" data-testid="watch-search-status">
              {watchStatus}
            </span>
          )}
          {saveStatus && (
            <span className="search-watch-status" data-testid="save-search-lens-status">
              {saveStatus}
            </span>
          )}
        </span>
      </div>
      {namedIndexError && <div className="picker-error">{namedIndexError}</div>}
      {anchorLabel && (
        <div className="palette-search-anchor" data-testid="search-anchor">
          <span>Similar to {anchorLabel}</span>
          <button type="button" className="icon-btn" aria-label="Clear similarity anchor" onClick={onClearAnchor}>
            <X size={12} />
          </button>
        </div>
      )}
      {searchError && <div className="picker-error">{searchError}</div>}
      {!searchError && status && (
        <div className="search-empty" role="status">
          {status}
          {setup && (
            <button
              type="button"
              className="mini-btn"
              data-testid="search-setup"
              disabled={setupBusy}
              onClick={() => onStartSetup(setup.setup_ref, setup.scope)}
            >
              {setupBusy ? 'Starting…' : 'Set up semantic search'}
            </button>
          )}
        </div>
      )}
      {setupError && <div className="picker-error">{setupError}</div>}
      {!searchError && groups !== null && (
        resultCount === 0 ? (
          coverage?.state !== 'preparing' && (
            <div className="search-empty">
              No matches{query.trim() ? ` for “${query.trim()}”` : ''}.
            </div>
          )
        ) : (
          <div className="search-results" data-testid="search-results">
            {groups.map((group) => (
              <div key={group.group_key} className="search-group">
                <div className="search-group-label">
                  {sheetName(group.primary.binding.sheet_id)}
                </div>
                <SearchHitRow
                  hit={group.primary}
                  supporting={false}
                  activeIndex={activeIndex}
                  rowIndex={rowIndexOfHit(group.primary)}
                  onHover={onHoverHit}
                  onActivate={onActivateHit}
                  onFindSimilar={onFindSimilar}
                />
                {group.supporting.map((hit) => (
                  <SearchHitRow
                    key={hitKey(hit)}
                    hit={hit}
                    supporting
                    activeIndex={activeIndex}
                    rowIndex={rowIndexOfHit(hit)}
                    onHover={onHoverHit}
                    onActivate={onActivateHit}
                    onFindSimilar={onFindSimilar}
                  />
                ))}
                {group.additional_count > 0 && (
                  <div className="search-group-more">
                    {group.additional_count} more related result{group.additional_count === 1 ? '' : 's'}
                  </div>
                )}
              </div>
            ))}
          </div>
        )
      )}
    </div>
  );
}
