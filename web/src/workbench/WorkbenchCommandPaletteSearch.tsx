import { Bell, Bookmark, LoaderCircle, SearchCheck, X } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import {
  getModelPull,
  orgGetModelPull,
  type EmbeddingIndexSummary,
  type ModelPullDto,
} from '../api/open';
import type {
  ProjectSearchGroupWire,
  ProjectSearchHitWire,
  ProjectSearchResponseWire,
} from '../api/projectSearch';
import { PanelSelect } from '../components/PanelSelect';
import { ModelPullProgress } from '../components/ModelPullProgress';
import type { PaletteSearchSetupOperation } from './usePaletteSearch';
import { decodeEscapedText } from '../displayText';
import { formatNumberDisplay } from '../format';

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
  const mediaGap = coverage.channels.find((channel) => (
    channel.channel === 'search_preparation'
    && channel.eligible_count != null
    && channel.prepared_count != null
    && channel.eligible_count > channel.prepared_count
  ));
  if (mediaGap && mediaGap.state !== 'pending') return 'Showing available search results.';
  if (coverage.state === 'preparing') return 'Preparing project search material…';
  if (coverage.state === 'partial') return 'Showing available search results.';
  if (coverage.state === 'failed') return 'Some project material could not be searched.';
  return coverage.channels.some((channel) => channel.setup)
    ? 'Set up semantic search to include similar material.'
    : 'No indexed results are available for this search.';
}

function hitLocationLabel(hit: ProjectSearchHitWire): string | null {
  const { pages, time } = hit.location;
  if (pages) return pages.start === pages.end
    ? `Page ${pages.start}`
    : `Pages ${pages.start}–${pages.end}`;
  if (time) return time.start_seconds === time.end_seconds
    ? `${time.start_seconds.toFixed(1)}s`
    : `${time.start_seconds.toFixed(1)}–${time.end_seconds.toFixed(1)}s`;
  return null;
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
  const locationLabel = hitLocationLabel(hit);
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
        <span className="search-hit-content">
          <span className="search-hit-heading">
            <span className="search-hit-col">
              {hit.source_name ?? hit.column_name}
            </span>
            {locationLabel && <span className="search-hit-location">{locationLabel}</span>}
          </span>
          <span className="search-hit-snip">
            <Snip text={hit.snippet} decodeEscapes={hit.ai_generated} />
          </span>
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
  setupOperation,
  onStartSetup,
  onFinishSetup,
  preparationBusy,
  preparationError,
  canPrepareGap,
  activeSheetId,
  onPrepareGap,
  preparationReview,
  onViewJobs,
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
  setupOperation: PaletteSearchSetupOperation | null;
  onStartSetup(setupRef: string, scope: 'workspace' | 'organization'): void;
  onFinishSetup(pull: ModelPullDto): void;
  preparationBusy: boolean;
  preparationError: string | null;
  canPrepareGap: boolean;
  activeSheetId: string | number | null | undefined;
  onPrepareGap(sheetId: number): void;
  preparationReview: ReactNode;
  onViewJobs(): void;
}) {
  const [setupConsentRef, setSetupConsentRef] = useState<string | null>(null);
  const [selectedGapSheetId, setSelectedGapSheetId] = useState('');
  const status = coverageMessage(coverage);
  const semanticUnavailable = coverage?.requested_mode !== 'lexical'
    ? coverage?.channels.find((channel) => (
      channel.channel === 'semantic'
      && channel.state === 'unavailable'
      && !channel.setup
      && channel.reason
    ))?.reason
    : null;
  const setup = coverage?.channels.find((channel) => channel.setup)?.setup;
  const operationForSetup = setupOperation?.setupRef === setup?.setup_ref
    ? setupOperation
    : null;
  const currentSetupOperation = operationForSetup?.pull.operation_kind === 'engine_setup'
    && operationForSetup.pull.model === setup?.setup_ref
    ? operationForSetup
    : null;
  const blockingSetupOperation = operationForSetup && !currentSetupOperation
    ? operationForSetup
    : null;
  const consentSetup = !operationForSetup && setup?.setup_ref === setupConsentRef
    ? setup
    : null;
  const resultCount = groups?.length ?? 0;
  const materialGap = coverage?.channels.find((channel) => (
    channel.channel === 'search_preparation'
    && channel.setup == null
    && (
      (channel.eligible_count != null
        && channel.prepared_count != null
        && channel.eligible_count > channel.prepared_count)
      || channel.state === 'unavailable'
      || channel.state === 'failed'
    )
  ));
  const materialGapCount = materialGap?.eligible_count != null && materialGap.prepared_count != null
    ? materialGap.eligible_count - materialGap.prepared_count
    : null;
  const gapSheetIds = materialGap?.channel === 'search_preparation'
    ? ((materialGap as typeof materialGap & { gap_sheet_ids?: number[] | null }).gap_sheet_ids ?? [])
    : [];
  const activeSheetKey = activeSheetId == null ? '' : String(activeSheetId);
  const gapSheetKeys = gapSheetIds.map(String);
  const effectiveGapSheetKey = gapSheetKeys.includes(selectedGapSheetId)
    ? selectedGapSheetId
    : gapSheetKeys.includes(activeSheetKey)
      ? activeSheetKey
      : gapSheetKeys[0] ?? '';

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
            aria-label={watchBusy ? 'Watching…' : 'Watch search'}
            aria-busy={watchBusy}
            disabled={!canWatch}
            onClick={onCreateWatch}
            title={anchored && namedIndexId
              ? 'Watch rows similar to this anchor'
              : mode === 'lexical'
                ? 'Watch this search'
                : 'Watch a keyword search'}
          >
            {watchBusy ? <LoaderCircle size={12} className="spin" aria-hidden /> : <Bell size={12} aria-hidden />}
          </button>
          <button
            type="button"
            className="mini-btn search-save-btn"
            data-testid="save-search-lens-button"
            aria-label={saveBusy ? 'Saving…' : 'Save view'}
            aria-busy={saveBusy}
            disabled={!canSaveLens}
            onClick={onSaveLens}
            title="Save this named-index search as a view"
          >
            {saveBusy ? <LoaderCircle size={12} className="spin" aria-hidden /> : <Bookmark size={12} aria-hidden />}
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
      {!searchError && semanticUnavailable && (
        <div className="search-empty" role="status" data-testid="search-semantic-unavailable">
          <span>{semanticUnavailable}</span>
          {coverage?.effective_modes.includes('lexical') && <span>Keyword search is available.</span>}
        </div>
      )}
      {!searchError && status && !preparationReview && !semanticUnavailable && (
        <div className="search-empty" role="status">
          {status}
          {setup && !operationForSetup && (
            <button
              type="button"
              className="mini-btn"
              data-testid="search-setup"
              disabled={setupBusy}
              onClick={() => setSetupConsentRef(setup.setup_ref)}
            >
              Set up semantic search
            </button>
          )}
        </div>
      )}
      {preparationReview}
      {consentSetup && (
        <section className="search-setup-consent" data-testid="search-setup-consent">
          <div>
            <strong>Download {consentSetup.display_name}?</strong>
            <span>
              Model download: {consentSetup.download_bytes == null
                ? 'Size not measured'
                : `About ${formatNumberDisplay(consentSetup.download_bytes, 'filesize')}`}
            </span>
          </div>
          <p>Search by meaning.</p>
          <div className="search-preparation-actions">
            <button
              type="button"
              className="btn"
              data-testid="search-setup-not-now"
              disabled={setupBusy}
              onClick={() => setSetupConsentRef(null)}
            >
              Not now
            </button>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="search-setup-confirm"
              disabled={setupBusy}
              onClick={() => onStartSetup(consentSetup.setup_ref, consentSetup.scope)}
            >
              {setupBusy ? 'Starting…' : 'Download and set up'}
            </button>
          </div>
        </section>
      )}
      {currentSetupOperation && currentSetupOperation.pull.status !== 'done' && (
        <section className="search-setup-consent" data-testid="search-setup-progress">
          <div>
            <strong>{currentSetupOperation.pull.status === 'failed' || currentSetupOperation.pull.status === 'cancelled'
              ? 'Semantic search setup needs attention'
              : 'Setting up semantic search'}</strong>
            <span>You can close Search and return here to check progress.</span>
          </div>
          <ModelPullProgress
            pull={currentSetupOperation.pull}
            fetchPull={currentSetupOperation.scope === 'organization'
              ? orgGetModelPull
              : getModelPull}
            onDone={onFinishSetup}
            onFailed={onFinishSetup}
            readOnly
          />
          {(currentSetupOperation.pull.status === 'failed'
            || currentSetupOperation.pull.status === 'cancelled') && (
            <button
              type="button"
              className="btn btn-primary"
              data-testid="search-setup-retry"
              disabled={setupBusy}
              onClick={() => onStartSetup(
                currentSetupOperation.setupRef,
                currentSetupOperation.scope,
              )}
            >
              {setupBusy ? 'Starting…' : 'Retry setup'}
            </button>
          )}
        </section>
      )}
      {blockingSetupOperation && (
        <section className="search-setup-consent" data-testid="search-setup-blocked-progress">
          <div>
            <strong>Another setup operation is active</strong>
            <span>Wait for it to finish, then try semantic setup again.</span>
          </div>
          <ModelPullProgress
            pull={blockingSetupOperation.pull}
            fetchPull={blockingSetupOperation.scope === 'organization'
              ? orgGetModelPull
              : getModelPull}
            onDone={onFinishSetup}
            onFailed={onFinishSetup}
            readOnly
          />
        </section>
      )}
      {setupError && <div className="picker-error">{setupError}</div>}
      {materialGap && !preparationReview && (
        <div className="search-preparation-gap" data-testid="search-preparation-gap">
          <span>
            {materialGap.channel === 'search_preparation' && materialGapCount != null
              ? `${materialGapCount.toLocaleString()} current media ${materialGapCount === 1 ? 'item needs' : 'items need'} search preparation.`
              : materialGapCount == null
              ? 'Some files need search preparation.'
              : `${materialGapCount.toLocaleString()} ${materialGapCount === 1 ? 'file needs' : 'files need'} search preparation.`}
          </span>
          {materialGap.state === 'pending' ? (
            <button
              type="button"
              className="mini-btn"
              data-testid="search-preparation-view-jobs"
              onClick={onViewJobs}
            >
              View Jobs
            </button>
          ) : effectiveGapSheetKey && (
            <>
              {gapSheetKeys.length > 1 && (
                <PanelSelect
                  className="row-height-select"
                  data-testid="search-preparation-sheet-select"
                  aria-label="Sheet to prepare"
                  value={effectiveGapSheetKey}
                  disabled={preparationBusy}
                  onChange={(event) => setSelectedGapSheetId(event.target.value)}
                >
                  {gapSheetIds.map((sheetId) => (
                    <option key={sheetId} value={sheetId}>{sheetName(sheetId)}</option>
                  ))}
                </PanelSelect>
              )}
              <button
                type="button"
                className="mini-btn"
                data-testid="search-preparation-gap-action"
                disabled={!canPrepareGap || preparationBusy}
                onClick={() => onPrepareGap(Number(effectiveGapSheetKey))}
              >
                {preparationBusy
                  ? 'Starting…'
                  : `Prepare ${sheetName(effectiveGapSheetKey)}`}
              </button>
            </>
          )}
        </div>
      )}
      {preparationError && <div className="picker-error">{preparationError}</div>}
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
