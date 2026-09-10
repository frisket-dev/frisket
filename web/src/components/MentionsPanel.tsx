// Preserve server ordering and counts; this read-only panel never starts extraction.
import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import { ChevronDown, ChevronRight, Filter, RefreshCw, Tags, X } from 'lucide-react';
import {
  type EntityMentionGroup,
  type EntityMentionTypeTotal,
  type EntityMentionsPreview,
  type GridFilterEntityValue,
  type GridFilterSpec,
  type SheetMeta,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { PanelError, PanelLoading } from './PanelPrimitives';
import { PanelSelect } from './PanelSelect';
import { panelErrorMessage, testIdKey } from './panelPrimitivesModel';
import { entityTypeLabel } from './action-panel/nerLabelModel';
import {
  MENTIONS_BLURB,
  MENTIONS_EMPTY_COLUMN_BLURB,
  activeGroupLabel,
  activeMentionFilter,
  appendMentionPage,
  coverageLine,
  eligibleMentionColumns,
  formsLabel,
  groupFilterValue,
  groupKey,
  mentionCountLabel,
  mentionFilterLabel,
  mentionTypeSections,
  mentionsZeroState,
  rowCountLabel,
  sameMentionFilter,
  surfaceFilterValue,
  typeFilterValue,
  uncoveredRows,
  type SheetColumn,
} from './mentionsPanelModel';

/** Search must remain server-side so it covers the whole sheet before paging. */
const SEARCH_DEBOUNCE_MS = 250;

const INITIAL_PER_TYPE = 25;

const PAGE_SIZE = 100;

export interface MentionsPanelProps {
  sheets: SheetMeta[];
  activeSheetId?: string | null;
  gridFilter?: GridFilterSpec | null;
  /** Data-version changes restart paging; filter and sort changes do not. */
  dataVersion?: number;
  onFilterEntity?(columnId: string, value: GridFilterEntityValue, valueLabel?: string): void;
  onClearFilter?(): void;
  /** Opens configuration only; never starts the action. */
  onExtractEntities?(): void;
}

type LoadStatus = 'idle' | 'loading' | 'loaded' | 'error';

interface MentionsPanelState {
  columnId: string;
  searchDraft: string;
  search: string;
  preview: EntityMentionsPreview | null;
  groups: EntityMentionGroup[];
  typeTotals: EntityMentionTypeTotal[];
  loadingTypes: string[];
  typeErrors: { type: string; message: string }[];
  status: LoadStatus;
  error: string | null;
  expanded: string[];
  collapsedTypes: string[];
  reloadSeq: number;
  /** Responses tagged with an obsolete query sequence must not append. */
  querySeq: number;
}

type MentionsPanelAction =
  | { type: 'selectColumn'; columnId: string }
  | { type: 'setSearchDraft'; value: string }
  | { type: 'commitSearch'; value: string }
  | { type: 'beginTypePage'; entityType: string }
  | { type: 'appendTypePage'; entityType: string; preview: EntityMentionsPreview; seq: number }
  | { type: 'reload' }
  | { type: 'loading' }
  | { type: 'loaded'; preview: EntityMentionsPreview }
  | { type: 'failed'; message: string }
  | { type: 'typePageFailed'; entityType: string; message: string; seq: number }
  | { type: 'toggleGroup'; key: string }
  | { type: 'toggleType'; entityType: string };

function initialState(): MentionsPanelState {
  return {
    columnId: '',
    searchDraft: '',
    search: '',
    preview: null,
    groups: [],
    typeTotals: [],
    loadingTypes: [],
    typeErrors: [],
    status: 'idle',
    error: null,
    expanded: [],
    collapsedTypes: [],
    reloadSeq: 0,
    querySeq: 0,
  };
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

function mergeTypeTotal(
  totals: EntityMentionTypeTotal[],
  entityType: string,
  total: number,
): EntityMentionTypeTotal[] {
  const next = totals.map((entry) =>
    (entry.type === entityType ? { type: entry.type, totalGroups: total } : entry));
  return next.some((entry) => entry.type === entityType)
    ? next
    : [...next, { type: entityType, totalGroups: total }];
}

function reduceMentionsPanel(
  state: MentionsPanelState,
  action: MentionsPanelAction,
): MentionsPanelState {
  switch (action.type) {
    case 'selectColumn':
      return {
        ...initialState(),
        columnId: action.columnId,
        querySeq: state.querySeq + 1,
      };
    case 'setSearchDraft':
      return { ...state, searchDraft: action.value };
    case 'commitSearch':
      return {
        ...state,
        search: action.value,
        groups: [],
        typeTotals: [],
        loadingTypes: [],
        typeErrors: [],
        querySeq: state.querySeq + 1,
      };
    case 'beginTypePage':
      return {
        ...state,
        loadingTypes: toggle(state.loadingTypes, action.entityType),
        typeErrors: state.typeErrors.filter((entry) => entry.type !== action.entityType),
      };
    case 'appendTypePage':
      if (action.seq !== state.querySeq) return state;
      return {
        ...state,
        groups: appendMentionPage(state.groups, action.preview.items),
        typeTotals: mergeTypeTotal(state.typeTotals, action.entityType, action.preview.totalGroups),
        loadingTypes: state.loadingTypes.filter((entityType) => entityType !== action.entityType),
      };
    case 'reload':
      return {
        ...state,
        loadingTypes: [],
        typeErrors: [],
        reloadSeq: state.reloadSeq + 1,
        querySeq: state.querySeq + 1,
      };
    case 'loading':
      return { ...state, status: 'loading', error: null };
    case 'loaded':
      return {
        ...state,
        status: 'loaded',
        preview: action.preview,
        groups: action.preview.items,
        typeTotals: action.preview.typeTotals,
        loadingTypes: [],
        typeErrors: [],
        error: null,
      };
    case 'failed':
      return { ...state, status: 'error', error: action.message, loadingTypes: [] };
    case 'typePageFailed': {
      if (action.seq !== state.querySeq) return state;
      const rest = state.typeErrors.filter((entry) => entry.type !== action.entityType);
      return {
        ...state,
        loadingTypes: state.loadingTypes.filter((entityType) => entityType !== action.entityType),
        typeErrors: [...rest, { type: action.entityType, message: action.message }],
      };
    }
    case 'toggleGroup':
      return { ...state, expanded: toggle(state.expanded, action.key) };
    case 'toggleType':
      return { ...state, collapsedTypes: toggle(state.collapsedTypes, action.entityType) };
  }
}

export function MentionsPanel({
  sheets,
  activeSheetId = null,
  gridFilter = null,
  dataVersion = 0,
  onFilterEntity,
  onClearFilter,
  onExtractEntities,
}: MentionsPanelProps) {
  const { projectApi } = useWorkspaceStores();
  const [state, dispatch] = useReducer(reduceMentionsPanel, undefined, initialState);
  const {
    columnId, searchDraft, search, preview, groups, typeTotals, loadingTypes, typeErrors,
    status, error, reloadSeq, querySeq,
  } = state;

  const sheet = useMemo(
    () => sheets.find((candidate) => candidate.id === activeSheetId) ?? null,
    [activeSheetId, sheets],
  );

  const eligibleColumns = useMemo(() => eligibleMentionColumns(sheet), [sheet]);
  const selectedColumn: SheetColumn | null =
    eligibleColumns.find((column) => column.id === columnId) ?? eligibleColumns[0] ?? null;

  useEffect(() => {
    if (searchDraft === search) return;
    const timer = window.setTimeout(() => {
      dispatch({ type: 'commitSearch', value: searchDraft });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchDraft, search]);

  const selectedColumnId = selectedColumn?.id ?? '';
  const selectedSheetId = sheet?.id ?? '';

  // Ignore the mount value to avoid a duplicate initial fetch.
  const seenDataVersion = useRef(dataVersion);
  useEffect(() => {
    if (seenDataVersion.current === dataVersion) return;
    seenDataVersion.current = dataVersion;
    dispatch({ type: 'reload' });
  }, [dataVersion]);

  useEffect(() => {
    if (!selectedColumnId || !selectedSheetId) return;
    let alive = true;
    dispatch({ type: 'loading' });
    const query = search.trim();
    void projectApi
      .entityMentionsPreview({
        sheetId: selectedSheetId,
        columnId: selectedColumnId,
        ...(query ? { search: query } : {}),
        limit: INITIAL_PER_TYPE,
        offset: 0,
      })
      .then((payload) => {
        if (alive) dispatch({ type: 'loaded', preview: payload });
      })
      .catch((failure: unknown) => {
        if (alive)
          dispatch({ type: 'failed', message: panelErrorMessage(failure, 'Mentions failed to load') });
      });
    return () => {
      alive = false;
    };
  }, [selectedColumnId, selectedSheetId, search, reloadSeq]);

  const loadTypePage = useCallback(
    (entityType: string, loaded: number) => {
      if (!selectedColumnId || !selectedSheetId) return;
      const seq = querySeq;
      dispatch({ type: 'beginTypePage', entityType });
      const query = search.trim();
      void projectApi
        .entityMentionsPreview({
          sheetId: selectedSheetId,
          columnId: selectedColumnId,
          ...(query ? { search: query } : {}),
          type: entityType,
          limit: PAGE_SIZE,
          offset: loaded,
        })
        .then((payload) => {
          dispatch({ type: 'appendTypePage', entityType, preview: payload, seq });
        })
        .catch((failure: unknown) => {
          dispatch({
            type: 'typePageFailed',
            entityType,
            message: panelErrorMessage(failure, 'Mentions failed to load'),
            seq,
          });
        });
    },
    [selectedColumnId, selectedSheetId, search, querySeq],
  );

  const canFilter = Boolean(onFilterEntity) && selectedColumn !== null;
  const activeFilter = selectedColumn
    ? activeMentionFilter(gridFilter, selectedColumn.name)
    : null;

  // Fingerprint selectors need the clicked group's spelling for user-facing filter copy.
  const activeLabel = activeGroupLabel(groups, activeFilter);

  const applyFilter = useCallback(
    (value: GridFilterEntityValue, valueLabel?: string) => {
      if (!selectedColumn || !onFilterEntity) return;
      onFilterEntity(selectedColumn.id, value, valueLabel);
    },
    [onFilterEntity, selectedColumn],
  );

  if (eligibleColumns.length === 0) {
    return <MentionsEmptyColumnState onExtractEntities={onExtractEntities} />;
  }

  const coverage = preview ? coverageLine(preview.coverage) : null;
  const uncovered = preview ? uncoveredRows(preview.coverage) : 0;
  const sections = mentionTypeSections(groups, typeTotals);
  const zeroState = preview ? mentionsZeroState(preview.totalGroups, preview.search) : null;

  return (
    <section className="sidebar-mentions" data-testid="mentions-panel">
      <p className="facets-blurb" data-testid="mentions-blurb">{MENTIONS_BLURB}</p>
      <div className="facets-body">
        <div className="facets-controls">
          {eligibleColumns.length > 1 && (
            <label>
              <span>Entity column</span>
              <PanelSelect
                className="form-input"
                data-testid="mentions-column-select"
                value={selectedColumn?.id ?? ''}
                onChange={(event) => dispatch({
                  type: 'selectColumn',
                  columnId: event.target.value,
                })}
              >
                {eligibleColumns.map((column) => (
                  <option key={column.id} value={column.id}>{column.name}</option>
                ))}
              </PanelSelect>
            </label>
          )}
          <label>
            <span>Search mentions</span>
            <input
              className="form-input"
              type="search"
              data-testid="mentions-search"
              placeholder="Search every spelling…"
              value={searchDraft}
              onChange={(event) => dispatch({
                type: 'setSearchDraft',
                value: event.target.value,
              })}
            />
          </label>
          <button
            type="button"
            className="icon-btn facets-refresh"
            data-testid="mentions-refresh"
            title="Refresh mentions"
            aria-label="Refresh mentions"
            onClick={() => dispatch({ type: 'reload' })}
          >
            <RefreshCw size={13} className={status === 'loading' ? 'spin' : undefined} />
          </button>
        </div>

        {coverage && (
          <div
            className="mentions-coverage"
            data-testid="mentions-coverage"
            data-uncovered={uncovered > 0 ? 'true' : undefined}
          >
            {coverage}
          </div>
        )}

        {activeFilter && (
          <div className="facets-active-filter" data-testid="mentions-active-filter">
            <Filter size={11} aria-hidden />
            <span
              className="facets-active-filter-label"
              title={mentionFilterLabel(activeFilter, activeLabel)}
            >
              {mentionFilterLabel(activeFilter, activeLabel)}
            </span>
            {onClearFilter && (
              <button
                type="button"
                className="facets-active-filter-clear"
                data-testid="mentions-active-filter-clear"
                aria-label="Clear mention filter"
                title="Clear mention filter"
                onClick={onClearFilter}
              >
                <X size={11} aria-hidden />
              </button>
            )}
          </div>
        )}

        {error !== null ? (
          <PanelError testId="mentions-error">{error}</PanelError>
        ) : preview === null ? (
          <PanelLoading testId="mentions-loading" label="Loading mentions…" />
        ) : (
          <>
            {zeroState !== null && (
              <div
                className="facets-summary"
                data-testid="mentions-summary"
                data-loading={status === 'loading' ? 'true' : undefined}
              >
                {zeroState}
              </div>
            )}
            {preview.totalGroups === 0 && !preview.search && onExtractEntities && (
              <div className="facets-note" data-testid="mentions-zero-groups-note">
                <button
                  type="button"
                  className="mentions-rerun-link"
                  data-testid="mentions-rerun-link"
                  onClick={onExtractEntities}
                >
                  Change the extraction settings and re-run map.ner
                </button>
                .
              </div>
            )}
            {sections.map((section) => (
              <TypeSection
                key={section.type}
                type={section.type}
                groups={section.groups}
                total={section.total}
                remaining={section.remaining}
                loading={loadingTypes.includes(section.type)}
                error={typeErrors.find((entry) => entry.type === section.type)?.message ?? null}
                collapsed={state.collapsedTypes.includes(section.type)}
                expandedGroups={state.expanded}
                activeFilter={activeFilter}
                canFilter={canFilter}
                onToggleType={() => dispatch({ type: 'toggleType', entityType: section.type })}
                onToggleGroup={(key) => dispatch({ type: 'toggleGroup', key })}
                onApplyFilter={applyFilter}
                onShowMore={() => loadTypePage(section.type, section.groups.length)}
              />
            ))}
          </>
        )}
      </div>
    </section>
  );
}

/** The CTA opens map.ner configuration and never launches work. */
function MentionsEmptyColumnState({
  onExtractEntities,
}: {
  onExtractEntities?(): void;
}) {
  return (
    <section className="sidebar-mentions" data-testid="mentions-panel">
      <p className="facets-blurb" data-testid="mentions-blurb">
        {MENTIONS_EMPTY_COLUMN_BLURB}
      </p>
      {onExtractEntities && (
        <button
          type="button"
          className="btn btn-primary mentions-cta"
          data-testid="mentions-extract-cta"
          onClick={onExtractEntities}
        >
          <Tags size={13} aria-hidden />
          Extract entities
        </button>
      )}
    </section>
  );
}

function TypeSection({
  type,
  groups,
  total,
  remaining,
  loading,
  error,
  collapsed,
  expandedGroups,
  activeFilter,
  canFilter,
  onToggleType,
  onToggleGroup,
  onApplyFilter,
  onShowMore,
}: {
  type: string;
  groups: EntityMentionGroup[];
  total: number;
  remaining: number;
  loading: boolean;
  error: string | null;
  collapsed: boolean;
  expandedGroups: string[];
  activeFilter: GridFilterEntityValue | null;
  canFilter: boolean;
  onToggleType(): void;
  onToggleGroup(key: string): void;
  onApplyFilter(value: GridFilterEntityValue, valueLabel?: string): void;
  onShowMore(): void;
}) {
  const typeValue = typeFilterValue(type);
  const typeActive = sameMentionFilter(activeFilter, typeValue);
  const typeName = entityTypeLabel(type);
  const step = Math.min(remaining, PAGE_SIZE);
  return (
    <div className="mentions-type-section" data-testid={`mentions-type-${testIdKey(type)}`}>
      <div className="mentions-type-heading">
        {/* The heading and its caret ONLY collapse/expand — filtering by the
            whole type is a separate, explicitly labelled affordance, so
            reading a section can never silently change the grid. */}
        <button
          type="button"
          className="mentions-type-toggle"
          data-testid={`mentions-type-toggle-${testIdKey(type)}`}
          aria-expanded={!collapsed}
          onClick={onToggleType}
        >
          {collapsed ? <ChevronRight size={12} aria-hidden /> : <ChevronDown size={12} aria-hidden />}
          <span className="mentions-type-name">{typeName}</span>
          {/* The category's TOTAL constrained count, from typeTotals — the
              section states its full size while holding only a page of it. */}
          <span className="facet-count">{total.toLocaleString()}</span>
        </button>
        <button
          type="button"
          className={`mentions-type-filter${typeActive ? ' is-active' : ''}`}
          data-testid={`mentions-type-filter-${testIdKey(type)}`}
          disabled={!canFilter}
          aria-pressed={typeActive}
          title={`Filter the grid to rows with any ${typeName} mention`}
          aria-label={`Filter the grid to rows with any ${typeName} mention`}
          onClick={() => onApplyFilter(typeValue)}
        >
          <Filter size={11} aria-hidden />
        </button>
      </div>
      {!collapsed && (
        <ul className="facets-list" data-testid={`mentions-groups-${testIdKey(type)}`}>
          {groups.map((group) => (
            <MentionGroupRow
              key={groupKey(group)}
              group={group}
              expanded={expandedGroups.includes(groupKey(group))}
              activeFilter={activeFilter}
              canFilter={canFilter}
              onToggle={() => onToggleGroup(groupKey(group))}
              onApplyFilter={onApplyFilter}
            />
          ))}
        </ul>
      )}
      {/* Paging is per category, INSIDE its section: this loads the next page of
          THIS type, so every category grows on its own rather than behind one
          global button the heaviest type monopolizes. The visible label is the
          step; the accessible name adds the category it grows.

          A failure lands here too, next to the button that caused it — the
          groups already loaded stay on screen, and the button becomes the retry
          rather than the panel becoming an error page. */}
      {!collapsed && (error !== null || remaining > 0) && (
        <div className="mentions-type-more">
          {error !== null && (
            <div
              className="panel-error mentions-type-error"
              data-testid={`mentions-show-more-error-${testIdKey(type)}`}
              role="status"
            >
              {error}
            </div>
          )}
          {remaining > 0 && (
            <button
              type="button"
              className="btn mentions-load-more"
              data-testid={`mentions-show-more-${testIdKey(type)}`}
              disabled={loading}
              title={
                error !== null
                  ? `Try again — show ${step.toLocaleString()} more ${typeName}`
                  : `Show ${step.toLocaleString()} more ${typeName}`
              }
              aria-label={
                error !== null
                  ? `Try again — show ${step.toLocaleString()} more ${typeName}`
                  : `Show ${step.toLocaleString()} more ${typeName}`
              }
              onClick={onShowMore}
            >
              {loading ? 'Loading…' : error !== null ? 'Try again' : `Show ${step.toLocaleString()} more`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function MentionGroupRow({
  group,
  expanded,
  activeFilter,
  canFilter,
  onToggle,
  onApplyFilter,
}: {
  group: EntityMentionGroup;
  expanded: boolean;
  activeFilter: GridFilterEntityValue | null;
  canFilter: boolean;
  onToggle(): void;
  onApplyFilter(value: GridFilterEntityValue, valueLabel?: string): void;
}) {
  const value = groupFilterValue(group);
  const isActive = sameMentionFilter(activeFilter, value);
  const key = testIdKey(groupKey(group));
  // A single-spelling group is a clean row; only a multi-spelling group earns
  // the "N forms" hint and its own disclosure caret, and that caret is
  // INDEPENDENT of the filter click.
  const multiSurface = group.surfaceCount > 1;
  return (
    <li className="facet-value-row" data-testid="mentions-group-row">
      <div className="mentions-group-line">
        {multiSurface ? (
          <button
            type="button"
            className="mentions-group-caret"
            data-testid={`mentions-group-expand-${key}`}
            aria-expanded={expanded}
            aria-label={`Show the ${formsLabel(group.surfaceCount)} grouped into ${group.label}`}
            title={`Show the ${formsLabel(group.surfaceCount)} grouped into ${group.label}`}
            onClick={onToggle}
          >
            {expanded ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
          </button>
        ) : (
          <span className="mentions-group-caret-spacer" aria-hidden />
        )}
        <button
          type="button"
          className={`facet-value-hit${isActive ? ' is-active' : ''}`}
          data-testid={`mentions-group-${key}`}
          disabled={!canFilter}
          aria-pressed={isActive}
          title={
            multiSurface
              ? `Filter the grid to rows mentioning any of the ${formsLabel(group.surfaceCount)} grouped as ${group.label}`
              : `Filter the grid to rows mentioning ${group.label}`
          }
          // The group's spelling travels with the click: the fingerprint in
          // `value` is a comparison token, so this is the only moment the
          // toolbar chip can learn what the user actually clicked.
          onClick={() => onApplyFilter(value, group.label)}
        >
          <span className="facet-label">{group.label}</span>
          {/* The counts share ONE row under the name, so the name — the thing
              being read — gets the panel's full width instead of competing
              with three short chips for it. */}
          <span className="mentions-row-meta">
            {multiSurface && (
              <span className="mentions-forms" data-testid={`mentions-forms-${key}`}>
                {formsLabel(group.surfaceCount)}
              </span>
            )}
            {/* Distinct rows is the PRIMARY number — it is what clicking
                produces. Occurrences ride along as secondary. */}
            <span className="facet-count">{rowCountLabel(group.rowCount)}</span>
            <span className="mentions-mention-count">{mentionCountLabel(group.mentionCount)}</span>
          </span>
        </button>
      </div>
      {multiSurface && expanded && (
        <ul className="mentions-surface-list" data-testid={`mentions-surfaces-${key}`}>
          {group.surfaces.map((surface) => {
            const surfaceValue = surfaceFilterValue(group.type, surface);
            const surfaceActive = sameMentionFilter(activeFilter, surfaceValue);
            return (
              <li className="facet-value-row" key={surface.text}>
                <button
                  type="button"
                  className={`facet-value-hit mentions-surface-hit${surfaceActive ? ' is-active' : ''}`}
                  data-testid={`mentions-surface-${testIdKey(surface.text) || 'blank'}`}
                  disabled={!canFilter}
                  aria-pressed={surfaceActive}
                  title={`Filter the grid to the exact spelling “${surface.text}”`}
                  onClick={() => onApplyFilter(surfaceValue)}
                >
                  {/* WHICH spellings were combined is the whole point of the
                      disclosure, so the spelling itself takes the room and
                      wraps rather than truncating to "AC…". */}
                  <span className="facet-label">{surface.text}</span>
                  <span className="mentions-row-meta">
                    <span className="mentions-exact-tag">exact spelling</span>
                    <span className="facet-count">{rowCountLabel(surface.rowCount)}</span>
                    <span className="mentions-mention-count">
                      {mentionCountLabel(surface.mentionCount)}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}
