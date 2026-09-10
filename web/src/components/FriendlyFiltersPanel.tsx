import { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronDown, Filter, RefreshCw, Search, X } from 'lucide-react';
import type {
  ColumnDef,
  ColumnDistribution,
  ColumnValuesPreview,
  GridFilterOperator,
  GridFilterListSelector,
  GridFilterSpec,
  GridFilterValue,
  SheetMeta,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { facetBehaviorFor } from '../grid/typeRegistry';
import { filterConditionFromDraft, gridFilterLabel } from '../workspace/gridColumnState';
import { filterOperatorOptions } from '../workspace/workspaceState';
import { entityTypeName } from './action-panel/nerLabelModel';
import { panelErrorMessage, testIdKey } from './panelPrimitivesModel';
import { PanelSelect } from './PanelSelect';

const ENUM_LIMIT = 20;
const SEARCH_LIMIT = 50;
const MAX_SELECTED_VALUES = 100;
const SEARCH_DEBOUNCE_MS = 250;
const MONTHS = Array.from({ length: 12 }, (_, index) => ({
  value: String(index + 1),
  label: new Intl.DateTimeFormat(undefined, { month: 'long', timeZone: 'UTC' }).format(
    new Date(Date.UTC(2020, index, 1)),
  ),
}));
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
  .map((label, index) => ({ value: String(index), label }));
const RELATIVE_UNITS = [
  { value: 'days', label: 'days' },
  { value: 'weeks', label: 'weeks' },
  { value: 'months', label: 'months' },
];
const DATE_PRESETS = new Set<GridFilterOperator>([
  'date_this_year',
  'date_ytd',
  'date_invalid',
]);

type FilterCondition = GridFilterSpec[string];

function isRangeValue(value: GridFilterValue | undefined): value is { start: string; end: string } {
  return typeof value === 'object' && value !== null && !Array.isArray(value) &&
    'start' in value && 'end' in value;
}

function isRelativeDateValue(
  value: GridFilterValue | undefined,
): value is { amount: number; unit: 'days' | 'weeks' | 'months' } {
  return typeof value === 'object' && value !== null && !Array.isArray(value) &&
    'amount' in value && 'unit' in value;
}

function calendarDateKey(value: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1 || month < 1 || month > 12 || day < 1) return null;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= days[month - 1] ? year * 10000 + month * 100 + day : null;
}

const CANONICAL_INTEGER = /^-?(?:0|[1-9]\d*)$/;
const MAX_SAFE_INTEGER_BIGINT = BigInt(Number.MAX_SAFE_INTEGER);
const MIN_SIGNED_64_BIT_INTEGER = -(2n ** 63n);
const MAX_SIGNED_64_BIT_INTEGER = 2n ** 63n - 1n;

function exactInteger(value: string): bigint | null {
  if (!CANONICAL_INTEGER.test(value)) return null;
  try {
    return BigInt(value);
  } catch {
    return null;
  }
}

function exactFilterInteger(value: string): bigint | null {
  const parsed = exactInteger(value);
  return parsed !== null
    && parsed >= MIN_SIGNED_64_BIT_INTEGER
    && parsed <= MAX_SIGNED_64_BIT_INTEGER
    ? parsed
    : null;
}

function safelySliderRepresentableInteger(value: string): boolean {
  const parsed = exactInteger(value);
  return parsed !== null
    && parsed >= -MAX_SAFE_INTEGER_BIGINT
    && parsed <= MAX_SAFE_INTEGER_BIGINT;
}

function safelySliderRepresentableIntegerRange(minimum: string, maximum: string): boolean {
  if (!safelySliderRepresentableInteger(minimum) || !safelySliderRepresentableInteger(maximum)) {
    return false;
  }
  const lower = BigInt(minimum);
  const upper = BigInt(maximum);
  return upper >= lower && upper - lower <= MAX_SAFE_INTEGER_BIGINT;
}

type RangeValueKind = 'date' | 'integer' | 'number';

function normalizeRangeValue(value: string, kind: RangeValueKind): string | null {
  if (kind === 'date') return calendarDateKey(value) === null ? null : value;
  const ungrouped = value.replaceAll(',', '').trim();
  if (!ungrouped) return null;
  if (kind === 'integer') {
    const parsed = exactFilterInteger(ungrouped);
    return parsed === null ? null : String(parsed);
  }
  const parsed = Number(ungrouped);
  return Number.isFinite(parsed) ? String(parsed) : null;
}

function compareRangeValues(left: string, right: string, kind: RangeValueKind): number {
  if (kind === 'integer') {
    const leftInteger = BigInt(left);
    const rightInteger = BigInt(right);
    return leftInteger < rightInteger ? -1 : leftInteger > rightInteger ? 1 : 0;
  }
  const leftNumber = kind === 'date' ? epochDay(left) : Number(left);
  const rightNumber = kind === 'date' ? epochDay(right) : Number(right);
  return leftNumber - rightNumber;
}

function formatRangeValue(value: string, kind: RangeValueKind): string {
  if (kind === 'date') return value;
  if (kind === 'integer') {
    const parsed = exactFilterInteger(value);
    return parsed === null ? value : parsed.toLocaleString();
  }
  const match = /^(-?)(\d+)(\.\d+)?$/.exec(value);
  if (!match) return value;
  return `${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${match[3] ?? ''}`;
}

function niceSliderStep(minimum: number, maximum: number): number {
  const span = maximum - minimum;
  const raw = Number.isFinite(span) ? span / 20 : maximum / 20 - minimum / 20;
  if (!Number.isFinite(raw) || raw <= 0) return Number.MIN_VALUE;
  const power = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / power;
  const factor = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  const step = factor * power;
  return Number.isFinite(step) && step > 0 ? step : Number.MIN_VALUE;
}

function numericSliderStops(
  minimum: number,
  maximum: number,
  selected: number[],
  integer: boolean,
): number[] {
  if (![minimum, maximum].every(Number.isFinite) || maximum <= minimum) return [minimum];
  const step = integer ? Math.max(1, niceSliderStep(minimum, maximum))
    : niceSliderStep(minimum, maximum);
  const values = [
    minimum,
    maximum,
    ...selected.filter((value) => Number.isFinite(value) && value >= minimum && value <= maximum),
  ];
  let value = Math.ceil(minimum / step) * step;
  for (let index = 0; index < 100 && value < maximum; index += 1) {
    if (value > minimum) values.push(Number(value.toPrecision(15)));
    value += step;
  }
  return [...new Set(values)].sort((left, right) => left - right);
}

function closestSliderStop(value: number, stops: number[], minimum: number, maximum: number): number {
  let closest = 0;
  let distance = Number.POSITIVE_INFINITY;
  for (let index = 0; index < stops.length; index += 1) {
    const candidateDistance = Math.abs(
      sliderPosition(value, minimum, maximum) - sliderPosition(stops[index], minimum, maximum),
    );
    if (candidateDistance < distance) {
      closest = index;
      distance = candidateDistance;
    }
  }
  return closest;
}

function defaultAdvancedFields(operator: GridFilterOperator): {
  value: string;
  start: string;
  end: string;
} {
  if (operator === 'date_relative') return { value: '30', start: 'days', end: '' };
  if (operator === 'date_year') {
    return { value: String(new Date().getUTCFullYear()), start: '', end: '' };
  }
  if (operator === 'date_month') {
    return { value: String(new Date().getUTCMonth() + 1), start: '', end: '' };
  }
  if (operator === 'date_weekday') return { value: '1', start: '', end: '' };
  return { value: '', start: '', end: '' };
}

function advancedValidation(
  columnType: ColumnDef['type'],
  operator: GridFilterOperator,
  value: string,
  start: string,
  end: string,
): string | null {
  if (columnType === 'date') {
    if (operator === 'between') {
      const startKey = calendarDateKey(start);
      const endKey = calendarDateKey(end);
      if (startKey === null || endKey === null) return 'Choose two valid calendar dates.';
      return startKey <= endKey ? null : 'The start date must be before the end date.';
    }
    if (operator === 'gte' || operator === 'lte') {
      return calendarDateKey(start) === null ? 'Choose a valid calendar date.' : null;
    }
    if (operator === 'eq' || operator === 'neq') {
      return calendarDateKey(value) === null ? 'Choose a valid calendar date.' : null;
    }
    if (operator === 'date_relative') {
      const amount = Number(value);
      if (!Number.isInteger(amount) || amount < 1 || amount > 10000) {
        return 'Enter a whole number from 1 to 10,000.';
      }
      return RELATIVE_UNITS.some((item) => item.value === start) ? null : 'Choose a time unit.';
    }
    if (operator === 'date_year') {
      const year = Number(value);
      return Number.isInteger(year) && year >= 1 && year <= 9999
        ? null
        : 'Enter a year from 1 to 9999.';
    }
    if (operator === 'date_month') {
      return MONTHS.some((item) => item.value === value) ? null : 'Choose a month.';
    }
    if (operator === 'date_weekday') {
      return WEEKDAYS.some((item) => item.value === value) ? null : 'Choose a day.';
    }
    return DATE_PRESETS.has(operator) ? null : 'Choose a date filter.';
  }
  const isInteger = columnType === 'integer';
  const isNumber = columnType === 'number';
  if (operator === 'between') {
    if (!start.trim() || !end.trim()) return 'Enter both bounds.';
    if (isInteger) {
      const startInteger = exactFilterInteger(start);
      const endInteger = exactFilterInteger(end);
      if (startInteger === null || endInteger === null) {
        return 'Enter signed 64-bit whole numbers in canonical form.';
      }
      return startInteger <= endInteger ? null : 'The minimum must not exceed the maximum.';
    }
    if (isNumber && (!Number.isFinite(Number(start)) || !Number.isFinite(Number(end)))) {
      return 'Enter two finite numbers.';
    }
    if (isNumber && Number(start) > Number(end)) return 'The minimum must not exceed the maximum.';
    return null;
  }
  const candidate = operator === 'gte' || operator === 'lte' ? start : value;
  if (!candidate.trim()) return 'Enter a value.';
  if (isInteger && exactFilterInteger(candidate) === null) {
    return 'Enter a signed 64-bit whole number in canonical form.';
  }
  if (isNumber && !Number.isFinite(Number(candidate))) return 'Enter a finite number.';
  return null;
}

export interface FriendlyFiltersPanelProps {
  sheets: SheetMeta[];
  activeSheetId?: string | null;
  gridFilter?: GridFilterSpec | null;
  dataVersion?: number;
  /** Atomic replacement is important: a series of single-column callbacks
   *  would race React's readback and lose one of two facets clicked quickly. */
  onApplyFilterSpec?(filter: GridFilterSpec | null): void;
}

function withColumnCondition(
  filter: GridFilterSpec | null | undefined,
  columnName: string,
  condition: FilterCondition | null,
): GridFilterSpec | null {
  const next: GridFilterSpec = { ...(filter ?? {}) };
  if (condition && Object.keys(condition).length > 0) next[columnName] = condition;
  else delete next[columnName];
  return Object.keys(next).length > 0 ? next : null;
}

function selectedValues(condition: FilterCondition | undefined): string[] {
  const inValues = condition?.in;
  if (Array.isArray(inValues)) return inValues.filter((value): value is string => typeof value === 'string');
  return typeof condition?.eq === 'string' ? [condition.eq] : [];
}

function listSelectorKey(selector: GridFilterListSelector): string {
  return selector.kind === 'scalar'
    ? `scalar:${typeof selector.value}:${JSON.stringify(selector.value)}`
    : `entity:${JSON.stringify(selector.type)}:${JSON.stringify(selector.text)}`;
}

function listSelectorLabel(selector: GridFilterListSelector): string {
  return selector.kind === 'scalar'
    ? String(selector.value)
    : `${selector.text} (${entityTypeName(selector.type)})`;
}

function isListSelector(value: unknown): value is GridFilterListSelector {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return (
    Object.keys(candidate).length === 2
    && candidate.kind === 'scalar'
    && (typeof candidate.value === 'string'
      || typeof candidate.value === 'boolean'
      || (typeof candidate.value === 'number' && Number.isFinite(candidate.value)))
  ) || (
    Object.keys(candidate).length === 3
    && candidate.kind === 'entity'
    && typeof candidate.type === 'string'
    && candidate.type !== ''
    && typeof candidate.text === 'string'
    && candidate.text !== ''
  );
}

function selectedListSelectors(condition: FilterCondition | undefined): GridFilterListSelector[] {
  const raw = condition?.list_contains_any;
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  return raw.flatMap((item) => {
    if (!isListSelector(item)) return [];
    const key = listSelectorKey(item);
    if (seen.has(key)) return [];
    seen.add(key);
    return [item];
  });
}

function conditionOperator(condition: FilterCondition | undefined): GridFilterOperator | null {
  for (const key of Object.keys(condition ?? {}) as GridFilterOperator[]) {
    if (condition?.[key] !== undefined && condition[key] !== null) return key;
  }
  return null;
}

export function FriendlyFiltersPanel({
  sheets,
  activeSheetId = null,
  gridFilter = null,
  dataVersion = 0,
  onApplyFilterSpec,
}: FriendlyFiltersPanelProps) {
  const activeSheet = sheets.find((sheet) => sheet.id === activeSheetId) ?? null;
  const [reloadSeq, setReloadSeq] = useState(0);
  const facetColumns = useMemo(
    () => [...(activeSheet?.columns ?? [])]
      .filter((column) => {
        const behavior = facetBehaviorFor(column.type);
        return behavior?.kind === 'range'
          || behavior?.kind === 'collection'
          || (behavior?.kind === 'categorical' && behavior.operator === 'eq');
      })
      .sort((left, right) =>
        Number(facetBehaviorFor(right.type)?.preferred === true)
        - Number(facetBehaviorFor(left.type)?.preferred === true)),
    [activeSheet],
  );
  const activeEntries = Object.entries(gridFilter ?? {});

  const updateColumn = useCallback(
    (columnName: string, condition: FilterCondition | null) => {
      onApplyFilterSpec?.(withColumnCondition(gridFilter, columnName, condition));
    },
    [gridFilter, onApplyFilterSpec],
  );

  if (!activeSheet) {
    return (
      <section className="sidebar-facets" data-testid="friendly-filters">
        <div className="facets-empty">Open a sheet to explore its filters.</div>
      </section>
    );
  }

  return (
    <section className="sidebar-facets friendly-filters" data-testid="friendly-filters">
      <div className="friendly-filter-intro">
        <div>
          <strong>Filter {activeSheet.name}</strong>
          <p data-testid="facets-blurb">
            Check values or narrow ranges. Values within a facet match either; facets combine.
          </p>
        </div>
        <button
          type="button"
          className="icon-btn facets-refresh"
          data-testid="facets-refresh"
          title="Refresh facets"
          aria-label="Refresh facets"
          onClick={() => setReloadSeq((value) => value + 1)}
        >
          <RefreshCw size={13} />
        </button>
      </div>

      <div className="friendly-facet-stack" data-testid="friendly-filters-panel">
        {facetColumns.map((column, index) => (
          <FacetCard
            key={column.id}
            sheet={activeSheet}
            column={column}
            condition={gridFilter?.[column.name]}
            defaultOpen={index < 4 || Boolean(gridFilter?.[column.name])}
            reloadKey={`${dataVersion}:${reloadSeq}`}
            canFilter={Boolean(onApplyFilterSpec)}
            onChange={(condition) => updateColumn(column.name, condition)}
          />
        ))}
        {facetColumns.length === 0 && (
          <div className="facets-empty">This sheet has no columns that can be faceted.</div>
        )}
      </div>

      {activeEntries.length > 0 && (
        <div className="friendly-active-filters" data-testid="facets-active-filters">
          <div className="friendly-active-heading">
            <span>{activeEntries.length} active {activeEntries.length === 1 ? 'filter' : 'filters'}</span>
            <button
              type="button"
              disabled={!onApplyFilterSpec}
              onClick={() => onApplyFilterSpec?.(null)}
            >
              Clear all
            </button>
          </div>
          {activeEntries.map(([column, condition]) => (
            <div className="facets-active-filter" key={column} data-testid="facets-active-filter">
              <Filter size={11} aria-hidden />
              <span className="facets-active-filter-label">
                {gridFilterLabel({ [column]: condition })}
              </span>
              <button
                type="button"
                className="facets-active-filter-clear"
                data-testid={`facets-clear-${testIdKey(column)}`}
                aria-label={`Clear ${column} filter`}
                title={`Clear ${column} filter`}
                disabled={!onApplyFilterSpec}
                onClick={() => updateColumn(column, null)}
              >
                <X size={13} aria-hidden />
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function FacetCard({
  sheet,
  column,
  condition,
  defaultOpen,
  reloadKey,
  canFilter,
  onChange,
}: {
  sheet: SheetMeta;
  column: ColumnDef;
  condition?: FilterCondition;
  defaultOpen: boolean;
  reloadKey: string;
  canFilter: boolean;
  onChange(condition: FilterCondition | null): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const [open, setOpen] = useState(defaultOpen);
  const [searchDraft, setSearchDraft] = useState('');
  const [search, setSearch] = useState('');
  const scopeKey = `${sheet.id}:${column.id}:${column.name}:${column.type}`;
  const requestKey = `${scopeKey}:${reloadKey}:${search.trim()}`;
  const [result, setResult] = useState<{
    key: string;
    scopeKey: string;
    preview: ColumnValuesPreview | null;
    error: string | null;
  } | null>(null);
  const scopedResult = result?.scopeKey === scopeKey ? result : null;
  const currentResult = scopedResult?.key === requestKey ? scopedResult : null;
  const preview = scopedResult?.preview ?? null;
  const error = currentResult?.error ?? null;
  const status = !open ? 'idle' : currentResult ? (error ? 'error' : 'loaded') : 'loading';
  const facetBehavior = facetBehaviorFor(column.type);
  const rangeColumn = facetBehavior?.kind === 'range';
  const collectionColumn = facetBehavior?.kind === 'collection';
  const operator = conditionOperator(condition);

  useEffect(() => {
    if (searchDraft === search) return;
    const timer = window.setTimeout(() => setSearch(searchDraft), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchDraft, search]);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    const query = search.trim();
    void projectApi
      .columnValuesPreview({
        sheetId: sheet.id,
        inputColumn: column.name,
        limit: query ? SEARCH_LIMIT : ENUM_LIMIT + 1,
        ...(query ? { search: query } : {}),
      })
      .then((result) => {
        if (!alive) return;
        setResult({ key: requestKey, scopeKey, preview: result, error: null });
      })
      .catch((reason: unknown) => {
        if (!alive) return;
        setResult({
          key: requestKey,
          scopeKey,
          preview: null,
          error: panelErrorMessage(reason, `Could not load ${column.name}`),
        });
      });
    return () => {
      alive = false;
    };
  }, [column.name, open, projectApi, requestKey, scopeKey, search, sheet.id]);

  return (
    <section
      className={`friendly-facet-card${operator ? ' is-active' : ''}`}
      data-testid={`friendly-facet-${testIdKey(column.name)}`}
    >
      <div className="friendly-facet-header">
        <button
          type="button"
          className="friendly-facet-toggle"
          data-testid={`facet-header-${testIdKey(column.name)}`}
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <span>
            <strong>{column.name}</strong>
            <small>
              {column.type}
              {preview
                ? ` · ${(collectionColumn ? preview.listFacet?.distinct ?? 0 : preview.distinct).toLocaleString()} values`
                : ''}
            </small>
          </span>
          <ChevronDown size={13} className={open ? 'is-open' : undefined} />
        </button>
        {operator && (
          <button
            type="button"
            className="friendly-facet-clear"
            aria-label={`Clear ${column.name} filter`}
            title={`Clear ${column.name} filter`}
            disabled={!canFilter}
            onClick={() => onChange(null)}
          >
            <X size={10} aria-hidden />
            <span>Clear</span>
          </button>
        )}
      </div>
      {open && (
        <div className="friendly-facet-body">
          {error && <div className="facets-error" role="alert">{error}</div>}
          {!preview && !error && <div className="facets-empty">Loading values…</div>}
          {preview && rangeColumn ? (
            <RangeFacet
              key={`${column.id}:${column.type}:${preview.valueHash}`}
              column={column}
              distribution={
                facetBehavior?.kind === 'range'
                && preview.distribution?.kind === facetBehavior.valueKind
                  ? preview.distribution
                  : null
              }
              condition={condition}
              canFilter={canFilter}
              onChange={onChange}
            />
          ) : preview && collectionColumn ? (
            <CollectionFacet
              column={column}
              preview={preview}
              condition={condition}
              search={searchDraft}
              loading={status === 'loading'}
              canFilter={canFilter}
              onSearch={setSearchDraft}
              onChange={onChange}
            />
          ) : preview ? (
            <CategoricalFacet
              column={column}
              preview={preview}
              condition={condition}
              search={searchDraft}
              loading={status === 'loading'}
              canFilter={canFilter}
              onSearch={setSearchDraft}
              onChange={onChange}
            />
          ) : null}
          {!collectionColumn && (
            <AdvancedConditionEditor
              key={`${column.id}:${column.type}:${JSON.stringify(condition ?? null)}`}
              column={column}
              condition={condition}
              canFilter={canFilter}
              onChange={onChange}
            />
          )}
        </div>
      )}
    </section>
  );
}

/** A collection facet works over members, not the JSON cell's serialized
 * whole value. The preview hands us both the stable visual key and the closed
 * selector; selection itself compares the selector shape so a server label or
 * key change cannot alter which row set remains checked. */
function CollectionFacet({
  column,
  preview,
  condition,
  search,
  loading,
  canFilter,
  onSearch,
  onChange,
}: {
  column: ColumnDef;
  preview: ColumnValuesPreview;
  condition?: FilterCondition;
  search: string;
  loading: boolean;
  canFilter: boolean;
  onSearch(value: string): void;
  onChange(condition: FilterCondition | null): void;
}) {
  const facet = preview.listFacet ?? null;
  const selected = selectedListSelectors(condition);
  const selectedKeys = new Set(selected.map(listSelectorKey));
  const searchable = Boolean(facet && (facet.distinct > ENUM_LIMIT || facet.search));
  const acceptedSearch = facet?.search?.trim() ?? '';
  const actionsStale = loading || search.trim() !== acceptedSearch;
  const shown = facet?.choices ?? [];
  const snapshotTooLarge = shown.length > MAX_SELECTED_VALUES;
  const shownKeys = new Set(shown.map((choice) => listSelectorKey(choice.selector)));
  const snapshotAlreadySelected = shown.length > 0
    && shown.length === selected.length
    && shown.every((choice) => selectedKeys.has(listSelectorKey(choice.selector)));
  const visible = [...shown];
  for (const selector of selected) {
    if (!shownKeys.has(listSelectorKey(selector))) {
      visible.unshift({
        key: listSelectorKey(selector),
        label: listSelectorLabel(selector),
        count: 0,
        selector,
      });
    }
  }

  const toggle = (selector: GridFilterListSelector) => {
    const key = listSelectorKey(selector);
    const next = selectedKeys.has(key)
      ? selected.filter((candidate) => listSelectorKey(candidate) !== key)
      : [...selected, selector];
    if (next.length === 0) onChange(null);
    else onChange({ list_contains_any: next.slice(0, MAX_SELECTED_VALUES) });
  };

  const selectAllShown = () => {
    if (!canFilter || actionsStale || shown.length === 0 || snapshotTooLarge) return;
    const next = [...selected];
    const nextKeys = new Set(selectedKeys);
    for (const choice of shown) {
      const key = listSelectorKey(choice.selector);
      if (!nextKeys.has(key)) {
        next.push(choice.selector);
        nextKeys.add(key);
      }
    }
    if (next.length > 0) onChange({ list_contains_any: next.slice(0, MAX_SELECTED_VALUES) });
  };

  if (!facet) {
    return <div className="facets-empty">No list values in this column.</div>;
  }

  const bulkNoteId = `facet-bulk-note-${testIdKey(column.name)}`;
  return (
    <>
      {searchable && (
        <label className="friendly-facet-search">
          <Search size={12} aria-hidden />
          <input
            type="search"
            value={search}
            data-testid={`facet-search-${testIdKey(column.name)}`}
            placeholder={`Find ${column.name}…`}
            onChange={(event) => onSearch(event.target.value)}
          />
        </label>
      )}
      <div
        className="friendly-facet-bulk-actions"
        role="group"
        aria-label={`Bulk actions for ${column.name}`}
        aria-describedby={bulkNoteId}
      >
        <button
          type="button"
          disabled={
            !canFilter
            || actionsStale
            || shown.length === 0
            || snapshotTooLarge
            || snapshotAlreadySelected
          }
          onClick={selectAllShown}
        >
          Select all shown
        </button>
      </div>
      <div
        id={bulkNoteId}
        className="friendly-facet-note"
        data-loading={loading ? 'true' : undefined}
      >
        {actionsStale ? 'Updating shown values… · ' : ''}
        {facet.truncated ? `Showing first ${shown.length.toLocaleString()} matches · ` : ''}
        counts across the full sheet
      </div>
      <div
        className={`friendly-checkbox-list${searchable ? ' is-searchable' : ''}`}
        data-testid={`facet-values-${testIdKey(column.name)}`}
        data-loading={loading ? 'true' : undefined}
        aria-busy={loading || undefined}
      >
        {visible.map((choice) => {
          const selectorKey = listSelectorKey(choice.selector);
          const isSelected = selectedKeys.has(selectorKey);
          // The service's label is sufficient for scalar members, but an
          // entity selector needs its type in the visible and accessible name:
          // the same text can legitimately be a Person and an Organization.
          const label = listSelectorLabel(choice.selector);
          return (
            <div key={`${choice.key}:${selectorKey}`} className="friendly-checkbox-row">
              <label className="friendly-checkbox-choice">
                <input
                  type="checkbox"
                  aria-label={label}
                  checked={isSelected}
                  disabled={!canFilter || (!isSelected && selected.length >= MAX_SELECTED_VALUES)}
                  data-testid={`facet-check-${testIdKey(column.name)}-${testIdKey(choice.key)}`}
                  onChange={() => toggle(choice.selector)}
                />
                <span title={label}>{label}</span>
              </label>
              <small>{choice.count > 0 ? choice.count.toLocaleString() : 'selected'}</small>
            </div>
          );
        })}
        {visible.length === 0 && (
          <div className="facets-empty">
            {facet.search ? `No values match “${facet.search}”.` : 'No list values in this column.'}
          </div>
        )}
      </div>
      {(selected.length >= MAX_SELECTED_VALUES || snapshotTooLarge) && (
        <div className="friendly-facet-note">Maximum of 100 selected values reached.</div>
      )}
      {!searchable && facet.distinct > visible.length && (
        <div className="friendly-facet-note">Search to browse the remaining values.</div>
      )}
    </>
  );
}

function CategoricalFacet({
  column,
  preview,
  condition,
  search,
  loading,
  canFilter,
  onSearch,
  onChange,
}: {
  column: ColumnDef;
  preview: ColumnValuesPreview;
  condition?: FilterCondition;
  search: string;
  loading: boolean;
  canFilter: boolean;
  onSearch(value: string): void;
  onChange(condition: FilterCondition | null): void;
}) {
  const selected = selectedValues(condition);
  const selectedSet = new Set(selected);
  const searchable = preview.distinct > ENUM_LIMIT || Boolean(preview.search);
  const shownValues = preview.values.map((entry) => entry.value);
  const acceptedSearch = preview.search?.trim() ?? '';
  const actionsStale = loading || search.trim() !== acceptedSearch;
  const snapshotTooLarge = shownValues.length > MAX_SELECTED_VALUES;
  const snapshotAlreadySelected = (
    (typeof condition?.eq === 'string' || Array.isArray(condition?.in))
    && selected.length === shownValues.length
    && shownValues.every((value) => selectedSet.has(value))
  );
  const bulkNoteId = `facet-bulk-note-${testIdKey(column.name)}`;
  const visible = [...preview.values];
  for (const value of selected) {
    if (!visible.some((entry) => entry.value === value)) visible.unshift({ value, count: 0 });
  }

  const toggle = (value: string) => {
    const next = selectedSet.has(value)
      ? selected.filter((candidate) => candidate !== value)
      : [...selected, value];
    if (next.length === 0) onChange(null);
    else if (next.length === 1) onChange({ eq: next[0] });
    else onChange({ in: next.slice(0, MAX_SELECTED_VALUES) });
  };

  const selectAllShown = () => {
    if (!canFilter || actionsStale || shownValues.length === 0 || snapshotTooLarge) return;
    if (shownValues.length === 1) onChange({ eq: shownValues[0] });
    else onChange({ in: shownValues });
  };

  const addSearchFilter = () => {
    if (!canFilter || actionsStale || !acceptedSearch) return;
    onChange({ contains: acceptedSearch });
  };

  return (
    <>
      {searchable && (
        <label className="friendly-facet-search">
          <Search size={12} aria-hidden />
          <input
            type="search"
            value={search}
            data-testid={`facet-search-${testIdKey(column.name)}`}
            placeholder={`Find ${column.name}…`}
            onChange={(event) => onSearch(event.target.value)}
          />
        </label>
      )}
      <div
        className="friendly-facet-bulk-actions"
        role="group"
        aria-label={`Bulk actions for ${column.name}`}
        aria-describedby={bulkNoteId}
      >
        <button
          type="button"
          disabled={
            !canFilter
            || actionsStale
            || shownValues.length === 0
            || snapshotTooLarge
            || snapshotAlreadySelected
          }
          onClick={selectAllShown}
        >
          Select all shown
        </button>
        {acceptedSearch && (
          <button
            type="button"
            disabled={!canFilter || actionsStale}
            onClick={addSearchFilter}
          >
            Add “{acceptedSearch}” filter
          </button>
        )}
      </div>
      <div
        id={bulkNoteId}
        className="friendly-facet-note"
        data-loading={loading ? 'true' : undefined}
      >
        {actionsStale ? 'Updating shown values… · ' : ''}
        {preview.truncated ? `Showing first ${shownValues.length.toLocaleString()} matches · ` : ''}
        {preview.missing > 0 ? `${preview.missing.toLocaleString()} blank · ` : ''}
        counts across the full sheet
      </div>
      <div
        className={`friendly-checkbox-list${searchable ? ' is-searchable' : ''}`}
        data-testid={`facet-values-${testIdKey(column.name)}`}
        data-loading={loading ? 'true' : undefined}
        aria-busy={loading || undefined}
      >
        {visible.map((entry) => {
          const isSelected = selectedSet.has(entry.value);
          return (
            <div key={entry.value} className="friendly-checkbox-row">
              <label className="friendly-checkbox-choice">
                <input
                  type="checkbox"
                  checked={isSelected}
                  disabled={
                    !canFilter
                    || (!isSelected && selected.length >= MAX_SELECTED_VALUES)
                  }
                  data-testid={`facet-check-${testIdKey(column.name)}-${testIdKey(entry.value) || 'blank'}`}
                  onChange={() => toggle(entry.value)}
                />
                <span title={entry.value}>{entry.value}</span>
              </label>
              <span className="friendly-checkbox-actions">
                <button
                  type="button"
                  disabled={!canFilter}
                  aria-label={`Only ${entry.value} in ${column.name}`}
                  onClick={() => onChange({ eq: entry.value })}
                >
                  only
                </button>
                {isSelected && (
                  <button
                    type="button"
                    disabled={!canFilter}
                    aria-label={`Remove ${entry.value} from ${column.name} filter`}
                    onClick={() => toggle(entry.value)}
                  >
                    <X size={11} aria-hidden />
                  </button>
                )}
              </span>
              <small>{entry.count > 0 ? entry.count.toLocaleString() : 'selected'}</small>
            </div>
          );
        })}
        {visible.length === 0 && (
          <div className="facets-empty">
            {preview.search ? `No values match “${preview.search}”.` : 'No values in this column.'}
          </div>
        )}
      </div>
      {(selected.length >= MAX_SELECTED_VALUES || snapshotTooLarge) && (
        <div className="friendly-facet-note">Maximum of 100 selected values reached.</div>
      )}
      {!searchable && preview.distinct > visible.length && (
        <div className="friendly-facet-note">Search to browse the remaining values.</div>
      )}
    </>
  );
}

function histogramBinExcluded(
  distribution: ColumnDistribution,
  bin: { start: number | string; end: number | string },
  start: string,
  end: string,
): boolean {
  if (distribution.kind === 'integer') {
    const selectedStart = exactFilterInteger(start);
    const selectedEnd = exactFilterInteger(end);
    const binStart = exactFilterInteger(String(bin.start));
    const binEnd = exactFilterInteger(String(bin.end));
    return selectedStart !== null
      && selectedEnd !== null
      && binStart !== null
      && binEnd !== null
      && selectedStart <= selectedEnd
      && (binEnd < selectedStart || binStart > selectedEnd);
  }
  const selectedStart = distribution.kind === 'date' ? epochDay(start) : Number(start);
  const selectedEnd = distribution.kind === 'date' ? epochDay(end) : Number(end);
  const binStart = distribution.kind === 'date'
    ? epochDay(String(bin.start))
    : Number(bin.start);
  const binEnd = distribution.kind === 'date' ? epochDay(String(bin.end)) : Number(bin.end);
  return [selectedStart, selectedEnd, binStart, binEnd].every(Number.isFinite)
    && selectedStart <= selectedEnd
    && (binEnd < selectedStart || binStart > selectedEnd);
}

function Histogram({
  distribution,
  start,
  end,
}: {
  distribution: ColumnDistribution;
  start: string;
  end: string;
}) {
  const max = Math.max(1, ...distribution.bins.map((bin) => bin.count));
  return (
    <div className="friendly-histogram" aria-label="Value distribution">
      {distribution.bins.map((bin, index) => {
        const excluded = histogramBinExcluded(distribution, bin, start, end);
        return (
          <span
            key={`${bin.start}:${bin.end}:${index}`}
            data-excluded={excluded ? 'true' : undefined}
            style={{ height: `${Math.max(4, (bin.count / max) * 100)}%` }}
            title={`${bin.start} – ${bin.end}: ${bin.count.toLocaleString()}`}
          />
        );
      })}
    </div>
  );
}

function epochDay(value: string): number {
  return Math.floor(new Date(`${value}T00:00:00Z`).getTime() / 86_400_000);
}

function dateFromEpochDay(value: number): string {
  return new Date(value * 86_400_000).toISOString().slice(0, 10);
}

function sliderPosition(value: number, minimum: number, maximum: number): number {
  if (![value, minimum, maximum].every(Number.isFinite) || maximum <= minimum) return 0;
  if (value <= minimum) return 0;
  if (value >= maximum) return 100;
  const scale = Math.max(Math.abs(value), Math.abs(minimum), Math.abs(maximum), Number.MIN_VALUE);
  const scaledMinimum = minimum / scale;
  const scaledSpan = maximum / scale - scaledMinimum;
  return scaledSpan > 0 ? ((value / scale - scaledMinimum) / scaledSpan) * 100 : 0;
}

function RangeFacet({
  column,
  distribution,
  condition,
  canFilter,
  onChange,
}: {
  column: ColumnDef;
  distribution: ColumnDistribution | null;
  condition?: FilterCondition;
  canFilter: boolean;
  onChange(condition: FilterCondition | null): void;
}) {
  const isDate = column.type === 'date';
  const isInteger = column.type === 'integer';
  const min = distribution?.min ?? '';
  const max = distribution?.max ?? '';
  const range = condition?.between;
  const initialStart = isRangeValue(range) ? String(range.start) : String(min);
  const initialEnd = isRangeValue(range) ? String(range.end) : String(max);
  const valueKind: RangeValueKind = isDate ? 'date' : isInteger ? 'integer' : 'number';
  const [start, setStart] = useState(initialStart);
  const [end, setEnd] = useState(initialEnd);
  const [startDraft, setStartDraft] = useState(() => formatRangeValue(initialStart, valueKind));
  const [endDraft, setEndDraft] = useState(() => formatRangeValue(initialEnd, valueKind));
  const externalRange = `${initialStart}\u0000${initialEnd}`;
  const [lastExternalRange, setLastExternalRange] = useState(externalRange);
  if (externalRange !== lastExternalRange) {
    setLastExternalRange(externalRange);
    setStart(initialStart);
    setEnd(initialEnd);
    setStartDraft(formatRangeValue(initialStart, valueKind));
    setEndDraft(formatRangeValue(initialEnd, valueKind));
  }

  if (!distribution) return <div className="facets-empty">No range values in this column.</div>;

  const exactSlider = !isInteger
    || safelySliderRepresentableIntegerRange(String(min), String(max));
  const numberMin = isDate ? epochDay(String(min)) : Number(min);
  const numberMax = isDate ? epochDay(String(max)) : Number(max);
  const startNumber = isDate ? epochDay(start) : Number(start);
  const endNumber = isDate ? epochDay(end) : Number(end);
  const sliderStops = isDate ? [] : numericSliderStops(
    numberMin,
    numberMax,
    [startNumber, endNumber],
    isInteger,
  );
  const sliderStartValue = Number.isFinite(startNumber)
    ? Math.max(numberMin, Math.min(startNumber, numberMax))
    : numberMin;
  const sliderEndValue = Number.isFinite(endNumber)
    ? Math.max(numberMin, Math.min(endNumber, numberMax))
    : numberMax;
  const sliderStart = isDate
    ? sliderStartValue
    : closestSliderStop(sliderStartValue, sliderStops, numberMin, numberMax);
  const sliderEnd = isDate
    ? sliderEndValue
    : closestSliderStop(sliderEndValue, sliderStops, numberMin, numberMax);
  const sliderMinimum = isDate ? numberMin : 0;
  const sliderMaximum = isDate ? numberMax : sliderStops.length - 1;
  const selectionStart = Math.min(sliderStartValue, sliderEndValue);
  const selectionEnd = Math.max(sliderStartValue, sliderEndValue);
  const selectionStartPercent = sliderPosition(selectionStart, numberMin, numberMax);
  const selectionEndPercent = numberMin === numberMax
    ? 100
    : sliderPosition(selectionEnd, numberMin, numberMax);

  const applyRange = (nextStart: string, nextEnd: string) => {
    setStart(nextStart);
    setEnd(nextEnd);
    setStartDraft(formatRangeValue(nextStart, valueKind));
    setEndDraft(formatRangeValue(nextEnd, valueKind));
    if (canFilter && (nextStart !== start || nextEnd !== end)) {
      onChange({ between: { start: nextStart, end: nextEnd } });
    }
  };

  const commitDraft = (bound: 'start' | 'end', draft: string, clamp: boolean) => {
    const normalized = normalizeRangeValue(draft, valueKind);
    const lower = normalizeRangeValue(String(min), valueKind);
    const upper = normalizeRangeValue(String(max), valueKind);
    if (normalized === null || lower === null || upper === null) {
      if (clamp) {
        if (bound === 'start') setStartDraft(formatRangeValue(start, valueKind));
        else setEndDraft(formatRangeValue(end, valueKind));
      }
      return;
    }
    const floor = bound === 'start' ? lower : start;
    const ceiling = bound === 'start' ? end : upper;
    let next = normalized;
    if (compareRangeValues(next, floor, valueKind) < 0) {
      if (!clamp) return;
      next = floor;
    }
    if (compareRangeValues(next, ceiling, valueKind) > 0) {
      if (!clamp) return;
      next = ceiling;
    }
    applyRange(bound === 'start' ? next : start, bound === 'end' ? next : end);
  };

  const setSliderStart = (value: number) => {
    const resolved = isDate ? dateFromEpochDay(value) : String(sliderStops[Math.round(value)]);
    const next = compareRangeValues(resolved, end, valueKind) > 0 ? end : resolved;
    applyRange(next, end);
  };
  const setSliderEnd = (value: number) => {
    const resolved = isDate ? dateFromEpochDay(value) : String(sliderStops[Math.round(value)]);
    const next = compareRangeValues(resolved, start, valueKind) < 0 ? start : resolved;
    applyRange(start, next);
  };

  return (
    <>
      <Histogram distribution={distribution} start={start} end={end} />
      <div className="friendly-range-inputs">
        <label>
          <span>Min</span>
          <input
            type={isDate ? 'date' : 'text'}
            inputMode={isInteger ? 'numeric' : isDate ? undefined : 'decimal'}
            value={startDraft}
            disabled={!canFilter}
            aria-label={`${column.name} minimum`}
            data-testid={`facet-range-start-${testIdKey(column.name)}`}
            onChange={(event) => {
              setStartDraft(event.target.value);
              commitDraft('start', event.target.value, false);
            }}
            onBlur={() => commitDraft('start', startDraft, true)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commitDraft('start', startDraft, true);
            }}
          />
        </label>
        <label>
          <span>Max</span>
          <input
            type={isDate ? 'date' : 'text'}
            inputMode={isInteger ? 'numeric' : isDate ? undefined : 'decimal'}
            value={endDraft}
            disabled={!canFilter}
            aria-label={`${column.name} maximum`}
            data-testid={`facet-range-end-${testIdKey(column.name)}`}
            onChange={(event) => {
              setEndDraft(event.target.value);
              commitDraft('end', event.target.value, false);
            }}
            onBlur={() => commitDraft('end', endDraft, true)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commitDraft('end', endDraft, true);
            }}
          />
        </label>
      </div>
      {exactSlider && (
        <div
          className="friendly-range-slider"
          role="group"
          aria-label={`${column.name} selected range`}
          data-testid={`facet-range-slider-${testIdKey(column.name)}`}
        >
          <div className="friendly-range-slider-track" aria-hidden>
            <span
              style={{
                left: `${selectionStartPercent}%`,
                right: `${100 - selectionEndPercent}%`,
              }}
            />
          </div>
          <input
            className="is-minimum"
            type="range"
            min={sliderMinimum}
            max={sliderMaximum}
            step={1}
            value={sliderStart}
            disabled={!canFilter}
            aria-label={`${column.name} minimum`}
            aria-valuemin={numberMin}
            aria-valuemax={numberMax}
            aria-valuenow={sliderStartValue}
            aria-valuetext={formatRangeValue(start, valueKind)}
            onChange={(event) => setSliderStart(Number(event.target.value))}
          />
          <input
            className="is-maximum"
            type="range"
            min={sliderMinimum}
            max={sliderMaximum}
            step={1}
            value={sliderEnd}
            disabled={!canFilter}
            aria-label={`${column.name} maximum`}
            aria-valuemin={numberMin}
            aria-valuemax={numberMax}
            aria-valuenow={sliderEndValue}
            aria-valuetext={formatRangeValue(end, valueKind)}
            onChange={(event) => setSliderEnd(Number(event.target.value))}
          />
        </div>
      )}
    </>
  );
}

function AdvancedConditionEditor({
  column,
  condition,
  canFilter,
  onChange,
}: {
  column: ColumnDef;
  condition?: FilterCondition;
  canFilter: boolean;
  onChange(condition: FilterCondition | null): void;
}) {
  const [open, setOpen] = useState(false);
  const options = filterOperatorOptions(column.type);
  const active = conditionOperator(condition);
  const initialOperator = options.some((option) => option.value === active) ? active! : options[0].value;
  const [operator, setOperator] = useState<GridFilterOperator>(initialOperator);
  const activeValue = active ? condition?.[active] : undefined;
  const defaults = defaultAdvancedFields(initialOperator);
  const initialValue = isRelativeDateValue(activeValue)
    ? String(activeValue.amount)
    : typeof activeValue === 'string' ? activeValue : defaults.value;
  const initialStart =
    active === 'between' && isRangeValue(activeValue)
      ? String(activeValue.start)
      : isRelativeDateValue(activeValue) ? activeValue.unit
      : active === 'gte' || active === 'lte' ? initialValue
      : defaults.start;
  const initialEnd =
    active === 'between' && isRangeValue(activeValue)
      ? String(activeValue.end)
      : '';
  const [value, setValue] = useState(initialValue);
  const [start, setStart] = useState(initialStart);
  const [end, setEnd] = useState(initialEnd);
  const range = operator === 'between';
  const bound = operator === 'gte' || operator === 'lte';
  const inputType = column.type === 'date' ? 'date' : column.type === 'number' ? 'number' : 'text';
  const inputMode = column.type === 'integer' ? 'numeric' : undefined;
  const validation = advancedValidation(column.type, operator, value, start, end);

  const apply = () => {
    if (validation) return;
    onChange(filterConditionFromDraft(operator, value, start, end));
  };

  const changeOperator = (next: string) => {
    const nextOperator = next as GridFilterOperator;
    const nextDefaults = defaultAdvancedFields(nextOperator);
    setOperator(nextOperator);
    setValue(nextDefaults.value);
    setStart(nextDefaults.start);
    setEnd(nextDefaults.end);
  };

  return (
    <div className="friendly-advanced">
      <button type="button" onClick={() => setOpen((value) => !value)}>
        {open ? 'Hide options' : 'More filter options'}
      </button>
      {open && (
        <div className="friendly-advanced-fields" data-testid={`facet-advanced-${testIdKey(column.name)}`}>
          <PanelSelect
            value={operator}
            testId={`facet-advanced-operator-${testIdKey(column.name)}`}
            ariaLabel={`${column.name} filter condition`}
            onValueChange={changeOperator}
            options={options}
          />
          {range ? (
            <div className="friendly-range-inputs">
              <input type={inputType} inputMode={inputMode} value={start} aria-label="Start" onChange={(event) => setStart(event.target.value)} />
              <input type={inputType} inputMode={inputMode} value={end} aria-label="End" onChange={(event) => setEnd(event.target.value)} />
            </div>
          ) : bound ? (
            <input type={inputType} inputMode={inputMode} value={start} aria-label="Value" onChange={(event) => setStart(event.target.value)} />
          ) : operator === 'date_relative' ? (
            <div className="friendly-range-inputs">
              <label>
                <span>Last</span>
                <input
                  type="number"
                  min="1"
                  max="10000"
                  step="1"
                  value={value}
                  data-testid={`facet-relative-amount-${testIdKey(column.name)}`}
                  onChange={(event) => setValue(event.target.value)}
                />
              </label>
              <label>
                <span>Unit</span>
                <PanelSelect
                  value={start}
                  testId={`facet-relative-unit-${testIdKey(column.name)}`}
                  ariaLabel="Relative date unit"
                  onValueChange={setStart}
                  options={RELATIVE_UNITS}
                />
              </label>
            </div>
          ) : operator === 'date_year' ? (
            <input
              type="number"
              min="1"
              max="9999"
              step="1"
              value={value}
              aria-label="Year"
              onChange={(event) => setValue(event.target.value)}
            />
          ) : operator === 'date_month' ? (
            <PanelSelect
              value={value}
              ariaLabel="Month"
              onValueChange={setValue}
              options={MONTHS}
            />
          ) : operator === 'date_weekday' ? (
            <PanelSelect
              value={value}
              ariaLabel="Day of week"
              onValueChange={setValue}
              options={WEEKDAYS}
            />
          ) : DATE_PRESETS.has(operator) ? (
            <p className="friendly-facet-note" data-testid={`facet-preset-hint-${testIdKey(column.name)}`}>
              {operator === 'date_invalid'
                ? 'Shows non-empty cells Frisket cannot parse as dates.'
                : 'This range updates automatically as time passes.'}
            </p>
          ) : (
            <input type={inputType} inputMode={inputMode} value={value} aria-label="Value" onChange={(event) => setValue(event.target.value)} />
          )}
          {validation && <span className="grid-filter-validation">{validation}</span>}
          <button
            type="button"
            className="btn"
            disabled={!canFilter || validation !== null}
            data-testid={`facet-advanced-apply-${testIdKey(column.name)}`}
            onClick={apply}
          >
            Apply
          </button>
        </div>
      )}
    </div>
  );
}
