// The bespoke drawer body for the Combine action ("Group values"). The core
// loop: select unassigned values (click toggle · shift range · ⌘ add) → ＋ New
// bucket / Add to ▾ → name the bucket (defaults to its most frequent member, ◆
// promotes another) → repeat, all inline, no modals and no drag-and-drop (marked
// v1 gap). Built on the shared resolve primitives (ValueList / SelectionBar /
// GroupCard). It emits canonical Params; the generated-action host owns
// request identity, output naming, preview, and execution.
//
// State scoping: the outer component owns what survives a column switch
// (the unmatched policy and value); everything
// column-scoped lives in the body below, keyed on `${sheet.id}:${column}`
// so a switch remounts (and thereby resets) it.
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { AlertTriangle, ChevronDown, Plus, Search } from 'lucide-react';
import { PanelSelect } from '../PanelSelect';
import {
  type ColumnValueCount,
} from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import type { GeneratedActionParamsBodyProps } from '../action-panel/GeneratedActionParamsBody';
import { GroupCard } from './GroupCard';
import { SelectionBar } from './SelectionBar';
import { ValueList } from './ValueList';
import {
  useColumnValuesPreview,
} from './useColumnValuesPreview';
import './combine-form.css';

export type CombineFormProps = GeneratedActionParamsBodyProps<'resolve.combine'>;

/** One authored bucket. `canonical` is free text (it starts as the most
 *  frequent member and ◆-promotion swaps it); `members` are exact cell
 *  values, each in AT MOST one bucket by construction (the unassigned list
 *  and the add-value search only ever offer un-bucketed values). */
interface Bucket {
  id: string;
  canonical: string;
  members: string[];
  collapsed: boolean;
}

/** Server max for /column-values/v1/preview — request the whole budget so
 *  `truncated` really means "the column has more than 2,000 distinct". */
const VALUE_LIMIT = 2000;
/** Server-side search (truncated columns only): debounce + page size. The
 *  search endpoint is case-insensitive substring, so a modest page covers
 *  the "rarer variant beyond the loaded 2,000" hunt. */
const SEARCH_DEBOUNCE_MS = 300;
const SEARCH_LIMIT = 50;

type UnmatchedPolicy = 'keep' | 'null' | 'value';

export function CombineForm(props: CombineFormProps) {
  if (!props.sheet) return <p className="form-hint">Choose a source sheet to configure this action.</p>;
  return <CombineFormForSheet {...props} sheet={props.sheet} />;
}

function CombineFormForSheet({
  sheet,
  params: canonical,
  setParams,
  Field,
}: CombineFormProps & { sheet: NonNullable<CombineFormProps['sheet']> }) {
  const canonicalGroups = Array.isArray(canonical?.groups)
    ? canonical.groups.flatMap((group) => {
        if (group === null || typeof group !== 'object' || Array.isArray(group)) return [];
        const value = group as Record<string, unknown>;
        return typeof value.canonical === 'string'
          && Array.isArray(value.members)
          && value.members.every((member) => typeof member === 'string')
          ? [{ canonical: value.canonical, members: [...value.members] as string[] }]
          : [];
      })
    : undefined;
  const canonicalUnmatched: UnmatchedPolicy = canonical?.unmatched === 'null'
    || canonical?.unmatched === 'value'
    ? canonical.unmatched
    : 'keep';
  const saved = canonicalGroups
    && Array.isArray(canonical?.groups)
    && canonicalGroups.length === canonical.groups.length
    ? {
        groups: canonicalGroups,
        unmatched: canonicalUnmatched,
        ...(typeof canonical?.unmatched_value === 'string'
          ? { unmatchedValue: canonical.unmatched_value }
          : {}),
    }
    : undefined;
  const initialColumn = typeof canonical.source === 'string'
    ? canonical.source
    : undefined;
  const activeColumn = typeof canonical.source === 'string' ? canonical.source : '';
  const paramsRef = useRef(canonical);
  useEffect(() => {
    paramsRef.current = canonical;
  }, [canonical]);
  const mergeParams = useCallback<CombineFormProps['setParams']>((owned) => {
    const next = { ...paramsRef.current, ...owned };
    if (owned.unmatched !== 'value') delete next.unmatched_value;
    setParams(next);
  }, [setParams]);
  // Cross-column state — a column switch keeps these.
  const [unmatched, setUnmatched] = useState<UnmatchedPolicy>(saved?.unmatched ?? 'keep');
  const [unmatchedValue, setUnmatchedValue] = useState(saved?.unmatchedValue ?? '');
  return (
    <CombineFormBody
      key={`${sheet.id}:${activeColumn}`}
      sheet={sheet}
      activeColumn={activeColumn}
      setParams={mergeParams}
      Field={Field}
      unmatched={unmatched}
      setUnmatched={setUnmatched}
      unmatchedValue={unmatchedValue}
      setUnmatchedValue={setUnmatchedValue}
      initialGroups={activeColumn === initialColumn ? saved?.groups : undefined}
    />
  );
}

interface CombineFormBodyProps {
  sheet: NonNullable<CombineFormProps['sheet']>;
  activeColumn: string;
  setParams: CombineFormProps['setParams'];
  Field: CombineFormProps['Field'];
  unmatched: UnmatchedPolicy;
  setUnmatched(next: UnmatchedPolicy): void;
  unmatchedValue: string;
  setUnmatchedValue(value: string): void;
  initialGroups?: Array<{ canonical: string; members: string[] }>;
}

function CombineFormBody({
  sheet,
  activeColumn,
  setParams,
  Field,
  unmatched,
  setUnmatched,
  unmatchedValue,
  setUnmatchedValue,
  initialGroups,
}: CombineFormBodyProps) {
  const { projectApi } = useWorkspaceStores();
  // ── authoring state ────────────────────────────────────────────────────
  const [buckets, setBuckets] = useState<Bucket[]>(() => (
    initialGroups?.map((group, index) => ({
      id: `bucket-${index + 1}`,
      canonical: group.canonical,
      members: [...group.members],
      collapsed: false,
    })) ?? []
  ));
  const [selected, setSelected] = useState<Set<string>>(new Set());
  /** The just-created bucket gets autoFocusName (name field focused). */
  const [freshBucketId, setFreshBucketId] = useState<string | null>(null);
  const [addQueries, setAddQueries] = useState<Record<string, string>>({});
  const [addToOpen, setAddToOpen] = useState(false);
  const bucketSeqRef = useRef(initialGroups?.length ?? 0);
  const addToRef = useRef<HTMLDivElement>(null);

  // ── server-side search (truncated enumerations only) ───────────────────
  // One limit-2000 page can't show every value of a big column, so when the
  // enumeration is `truncated` the Unassigned search box and each bucket's
  // add-value box ALSO query the endpoint's server-side substring search.
  // Results are cached per trimmed query and merged with the local matches;
  // their counts extend `countByValue` so beyond-page picks are groupable
  // with real totals. When NOT truncated everything stays client-side.
  /** The Unassigned list's query (owned here, not by ValueList, so it can
   *  drive the server search too). */
  const [unassignedQuery, setUnassignedQuery] = useState('');
  /** The most recently edited query across ALL search boxes — the debounced
   *  fetch trigger. */
  const [serverQuery, setServerQuery] = useState('');
  /** Fetched pages keyed by trimmed query text. */
  const [serverMatches, setServerMatches] = useState<Map<string, ColumnValueCount[]>>(
    () => new Map(),
  );
  /** Counts for every server-fetched value — extends `countByValue` so
   *  bucket totals and the footer arithmetic treat beyond-page members
   *  exactly like loaded ones. */
  const [fetchedCounts, setFetchedCounts] = useState<Map<string, number>>(() => new Map());
  /** A server search answered from a LATER value inventory (valueHash
   *  mismatch) — surface the reload affordance instead of silently mixing
   *  snapshots. */
  const [searchStale, setSearchStale] = useState(false);
  /** Monotonic search-request id: a slower earlier search can never land on
   *  top of a newer one; a reload bumps it so in-flight searches against the
   *  old snapshot are discarded. */
  const searchSeqRef = useRef(0);

  // ── enumeration (read-only preview) ────────────────────────────────────
  const { preview, loading, error: loadError, reloadNotice, reload } = useColumnValuesPreview({
    sheetId: sheet.id,
    column: activeColumn,
    limit: VALUE_LIMIT,
    droppedNoun: 'grouped value',
    // Reload keeps the authored buckets (the stale-replay recovery path);
    // buckets are re-keyed by value — members no longer in the column are
    // dropped (counted for the notice), an emptied bucket dissolves, and the
    // selection sheds vanished values. The fresh enumeration is a NEW
    // snapshot — earlier server-search results (and any stale signal) no
    // longer apply; live queries re-fetch against the fresh hash.
    pruneOnReload: (out) => {
      const known = new Set(out.values.map((v) => v.value));
      let dropped = 0;
      const nextBuckets = buckets.flatMap((bucket) => {
        const members = bucket.members.filter((member) => known.has(member));
        dropped += bucket.members.length - members.length;
        return members.length === 0 ? [] : [{ ...bucket, members }];
      });
      setBuckets(nextBuckets);
      setSelected((prev) => new Set([...prev].filter((value) => known.has(value))));
      setServerMatches(new Map());
      setFetchedCounts(new Map());
      setSearchStale(false);
      searchSeqRef.current += 1;
      return dropped;
    },
  });

  const truncated = preview?.truncated ?? false;
  const previewHash = preview?.valueHash ?? null;

  useEffect(() => {
    if (!truncated || searchStale) return;
    const q = serverQuery.trim();
    if (q === '' || serverMatches.has(q)) return;
    const timer = window.setTimeout(() => {
      const seq = (searchSeqRef.current += 1);
      projectApi
        .columnValuesPreview({
          sheetId: sheet.id,
          inputColumn: activeColumn,
          search: q,
          limit: SEARCH_LIMIT,
        })
        .then((out) => {
          if (seq !== searchSeqRef.current) return;
          if (previewHash !== null && out.valueHash !== previewHash) {
            // Same column family, later read — a stale enumeration signal.
            setSearchStale(true);
            return;
          }
          setServerMatches((prev) => new Map(prev).set(q, out.values));
          setFetchedCounts((prev) => {
            const next = new Map(prev);
            for (const entry of out.values) {
              if (!next.has(entry.value)) next.set(entry.value, entry.count);
            }
            return next;
          });
        })
        .catch(() => {
          // Server search is additive — local matches still render; the next
          // keystroke retries.
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [truncated, searchStale, serverQuery, serverMatches, previewHash, sheet.id, activeColumn,
    projectApi]);

  // Close the Add to ▾ menu on outside click / Escape.
  useEffect(() => {
    if (!addToOpen) return;
    const onPointerDown = (event: globalThis.MouseEvent) => {
      const wrap = addToRef.current;
      if (wrap && event.target instanceof Node && !wrap.contains(event.target)) {
        setAddToOpen(false);
      }
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setAddToOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [addToOpen]);

  // ── derived ────────────────────────────────────────────────────────────
  /** Loaded-page counts EXTENDED with server-search hits, so a beyond-page
   *  member contributes its real count to bucket totals and the footer. */
  const countByValue = useMemo(() => {
    const map = new Map<string, number>(fetchedCounts);
    for (const entry of preview?.values ?? []) map.set(entry.value, entry.count);
    return map;
  }, [preview, fetchedCounts]);

  const loadedValueSet = useMemo(
    () => new Set((preview?.values ?? []).map((entry) => entry.value)),
    [preview],
  );

  const assigned = useMemo(() => {
    const set = new Set<string>();
    for (const bucket of buckets) for (const member of bucket.members) set.add(member);
    return set;
  }, [buckets]);

  /** Server-search hits for `rawQuery` that the loaded page does NOT already
   *  show and no bucket has claimed — the mergeable unmatched. */
  const fetchedMatchesFor = (rawQuery: string): ColumnValueCount[] => {
    if (!truncated) return [];
    const q = rawQuery.trim();
    if (q === '') return [];
    return (serverMatches.get(q) ?? []).filter(
      (entry) => !loadedValueSet.has(entry.value) && !assigned.has(entry.value),
    );
  };

  const unassignedEntries = useMemo(
    () => (preview?.values ?? []).filter((entry) => !assigned.has(entry.value)),
    [preview, assigned],
  );

  /** What the Unassigned ValueList shows. Non-truncated: the derived list
   *  (ValueList's own client filter handles search — no extra requests).
   *  Truncated: the query lives HERE so it can drive the server search;
   *  local matches merge with beyond-page server hits. */
  const displayedUnassigned = useMemo(() => {
    if (!truncated) return unassignedEntries;
    const q = unassignedQuery.trim().toLowerCase();
    if (q === '') return unassignedEntries;
    const local = unassignedEntries.filter((entry) => entry.value.toLowerCase().includes(q));
    const fetched = (serverMatches.get(unassignedQuery.trim()) ?? []).filter(
      (entry) => !loadedValueSet.has(entry.value) && !assigned.has(entry.value),
    );
    return [...local, ...fetched];
  }, [truncated, unassignedEntries, unassignedQuery, serverMatches, loadedValueSet, assigned]);

  /** Canonicals shared by ≥2 buckets (case-sensitive exact) → merge nudge. */
  const duplicateNames = useMemo(() => {
    const byName = new Map<string, number>();
    for (const bucket of buckets) {
      byName.set(bucket.canonical, (byName.get(bucket.canonical) ?? 0) + 1);
    }
    return [...byName.entries()]
      .filter(([, n]) => n > 1)
      .map(([name, n]) => ({ name, n }));
  }, [buckets]);

  const groupedCount = assigned.size;
  const keptCount = preview ? Math.max(preview.distinct - groupedCount, 0) : 0;

  // ── bucket operations ──────────────────────────────────────────────────
  /** Selected values in enumeration order (count desc, value asc) — so a new
   *  bucket's first member IS its most frequent one. Server-search picks
   *  beyond the loaded page follow, ordered by their fetched counts (the
   *  loaded page is the column's frequency head, so loaded-first keeps the
   *  overall count-desc shape). */
  const selectionInOrder = (): string[] => {
    const inPage = (preview?.values ?? [])
      .filter((entry) => selected.has(entry.value))
      .map((entry) => entry.value);
    const seen = new Set(inPage);
    const beyond = [...selected]
      .filter((value) => !seen.has(value))
      .sort((a, b) => (countByValue.get(b) ?? 0) - (countByValue.get(a) ?? 0));
    return [...inPage, ...beyond];
  };

  /** ＋ New bucket: members leave Unassigned, the group jumps to the
   *  top, the name defaults to the most frequent member and auto-focuses. */
  const createBucket = () => {
    const members = selectionInOrder();
    if (members.length === 0) return;
    const id = `bucket-${(bucketSeqRef.current += 1)}`;
    setBuckets((prev) => [{ id, canonical: members[0], members, collapsed: false }, ...prev]);
    setFreshBucketId(id);
    setSelected(new Set());
  };

  /** Add to ▾ appends the selection to an existing bucket. */
  const addSelectionTo = (bucketId: string) => {
    const additions = selectionInOrder();
    if (additions.length === 0) return;
    setBuckets((prev) => prev.map((bucket) =>
      bucket.id === bucketId
        ? {
            ...bucket,
            members: [
              ...bucket.members,
              ...additions.filter((value) => !bucket.members.includes(value)),
            ],
          }
        : bucket));
    setSelected(new Set());
    setAddToOpen(false);
  };

  /** The bucket's own ⌕ add-value box. */
  const addValueToBucket = (bucketId: string, value: string) => {
    setBuckets((prev) => prev.map((bucket) =>
      bucket.id === bucketId && !bucket.members.includes(value)
        ? { ...bucket, members: [...bucket.members, value] }
        : bucket));
    setAddQueries((prev) => ({ ...prev, [bucketId]: '' }));
  };

  /** × returns the member to Unassigned (implicitly: the unassigned
   *  list is derived); an emptied bucket auto-dissolves. */
  const removeMember = (bucketId: string, value: string) => {
    setBuckets((prev) => prev.flatMap((bucket) => {
      if (bucket.id !== bucketId) return [bucket];
      const members = bucket.members.filter((member) => member !== value);
      return members.length === 0 ? [] : [{ ...bucket, members }];
    }));
  };

  const deleteBucket = (bucketId: string) => {
    setBuckets((prev) => prev.filter((bucket) => bucket.id !== bucketId));
  };

  const renameBucket = (bucketId: string, canonical: string) => {
    setBuckets((prev) => prev.map((bucket) =>
      bucket.id === bucketId ? { ...bucket, canonical } : bucket));
  };

  const setCollapsed = (bucketId: string, collapsed: boolean) => {
    setBuckets((prev) => prev.map((bucket) =>
      bucket.id === bucketId ? { ...bucket, collapsed } : bucket));
  };

  /** Merge every bucket sharing `name` into the earliest one (the
   *  backend allows duplicate canonicals as a union, but the UI nudges). */
  const mergeDuplicates = (name: string) => {
    setBuckets((prev) => {
      const matching = prev.filter((bucket) => bucket.canonical === name);
      if (matching.length < 2) return prev;
      const merged: Bucket = {
        ...matching[0],
        members: [...new Set(matching.flatMap((bucket) => bucket.members))],
      };
      return prev.flatMap((bucket) => {
        if (bucket.canonical !== name) return [bucket];
        return bucket.id === merged.id ? [merged] : [];
      });
    });
  };

  /** Fallback for an empty confirmed name: the most frequent member (ties →
   *  the earlier member, which creation order already puts first). */
  const defaultNameFor = (bucket: Bucket): string =>
    bucket.members.reduce(
      (best, member) =>
        (countByValue.get(member) ?? 0) > (countByValue.get(best) ?? 0) ? member : best,
      bucket.members[0] ?? bucket.canonical,
    );

  // ── commit ─────────────────────────────────────────────────────────────
  const nonEmptyBuckets = useMemo(
    () => buckets.filter((bucket) => bucket.members.length > 0),
    [buckets],
  );
  useEffect(() => {
    setParams({
      source: activeColumn,
      // Zero-member buckets are dropped (auto-dissolve should have removed
      // them already); the emitted groups mirror bucket order exactly.
      groups: nonEmptyBuckets.map((bucket) => ({
        canonical: bucket.canonical,
        members: bucket.members,
      })),
      // Policy is always sent explicitly (wire-legibility: no default drift).
      unmatched,
      ...(unmatched === 'value' ? { unmatched_value: unmatchedValue } : {}),
    });
  }, [activeColumn, nonEmptyBuckets, setParams, unmatched, unmatchedValue]);

  // The unmatched policy is NOT here: it decides what happens to every
  // ungrouped value (including the destructive "set null"), so it renders as
  // an always-visible select next to the other settings — same posture as
  // SubstituteForm's "Values not listed" select.
  // Footer cost line: "46 → 2 groups · 96 kept · 2,542 rows →
  // employer_clean"; before any grouping, an honest zero state instead.
  // The unmatched segment tells the truth about the active policy: "kept"
  // only under keep — null renders "set null", value renders "→ {value}".
  const unmatchedSegment =
    unmatched === 'null'
      ? `${keptCount.toLocaleString()} set null`
      : unmatched === 'value'
        ? `${keptCount.toLocaleString()} → ${unmatchedValue.trim() || '…'}`
        : `${keptCount.toLocaleString()} kept`;
  const summary = !preview
    ? null
    : groupedCount === 0
      ? `Nothing grouped yet · ${preview.distinct.toLocaleString()} kept as-is — click rows to select (shift for a range), then group.`
      : `${groupedCount.toLocaleString()} → ${nonEmptyBuckets.length.toLocaleString()} ${nonEmptyBuckets.length === 1 ? 'group' : 'groups'} · ${unmatchedSegment} · ${preview.totalRows.toLocaleString()} rows`;

  const scopeMeta = !preview
    ? null
    : buckets.length > 0
      ? `${buckets.length} ${buckets.length === 1 ? 'group' : 'groups'} · ${keptCount.toLocaleString()} left`
      : `${preview.distinct.toLocaleString()} distinct · by freq`;

  // ── per-bucket inline ⌕ add-value box (GroupCard children slot) ────────
  const renderAddValueBox = (bucket: Bucket) => {
    const query = addQueries[bucket.id] ?? '';
    const q = query.trim().toLowerCase();
    // Only values still unassigned (in NO bucket) are offered. Under
    // a truncated enumeration the server-search hits beyond the loaded page
    // join the local matches.
    const matches = q
      ? [
          ...unassignedEntries.filter((entry) => entry.value.toLowerCase().includes(q)),
          ...fetchedMatchesFor(query),
        ].slice(0, 8)
      : [];
    return (
      <div className="resolve-combine-add-value" data-testid="resolve-combine-bucket-add-value">
        <div className="resolve-combine-add-value-row">
          <Search size={12} aria-hidden />
          <input
            className="form-input resolve-combine-add-value-input"
            data-testid="resolve-combine-bucket-add-value-input"
            value={query}
            placeholder="Add a value"
            aria-label={`Search unassigned values to add to ${bucket.canonical}`}
            onChange={(event) => {
              setAddQueries((prev) => ({ ...prev, [bucket.id]: event.target.value }));
              if (truncated) setServerQuery(event.target.value);
            }}
          />
          <span className="resolve-combine-add-value-hint">add to this group</span>
        </div>
        {q !== '' && matches.length > 0 && (
          <div className="resolve-combine-add-value-options">
            {matches.map((entry) => (
              <button
                key={entry.value}
                type="button"
                data-testid="resolve-combine-bucket-add-value-option"
                onClick={() => addValueToBucket(bucket.id, entry.value)}
              >
                <Plus size={11} aria-hidden />
                <span className="resolve-combine-add-value-name" title={entry.value}>
                  {entry.value}
                </span>
                <span className="resolve-combine-add-value-count">
                  {entry.count.toLocaleString()}
                </span>
              </button>
            ))}
          </div>
        )}
        {q !== '' && matches.length === 0 && (
          <div className="resolve-combine-add-value-none" data-testid="resolve-combine-bucket-add-value-none">
            No unassigned values match
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="resolve-combine-form" data-testid="resolve-combine-form">
      <div className="resolve-combine-scope">
        <div className="field-group resolve-combine-column">
          <Field name="source" testId="resolve-combine-column-select" />
        </div>
        {scopeMeta && (
          <span className="resolve-combine-scope-meta" data-testid="resolve-combine-scope-meta">
            {scopeMeta}
          </span>
        )}
        {preview && (
          <button
            type="button"
            className="btn resolve-combine-reload"
            data-testid="resolve-combine-reload"
            title="Re-fetch column values — keeps your groups; use after the source changed under a preview"
            onClick={reload}
          >
            Reload values
          </button>
        )}
      </div>

      {reloadNotice && (
        <div
          className="form-hint resolve-combine-reload-notice"
          data-testid="resolve-combine-reload-notice"
          role="status"
        >
          {reloadNotice}
        </div>
      )}

      <div className="resolve-combine-body">
          <SelectionBar count={selected.size} onClear={() => setSelected(new Set())}>
            <button
              type="button"
              className="btn btn-primary resolve-combine-bar-btn"
              data-testid="resolve-combine-new-bucket"
              onClick={createBucket}
            >
              <Plus size={12} /> New bucket
            </button>
            <div className="resolve-combine-add-to-wrap" ref={addToRef}>
              <button
                type="button"
                className="btn resolve-combine-bar-btn"
                data-testid="resolve-combine-add-to"
                disabled={buckets.length === 0}
                title={buckets.length === 0 ? 'No buckets yet — create one first' : undefined}
                aria-haspopup="menu"
                aria-expanded={addToOpen}
                onClick={() => setAddToOpen((open) => !open)}
              >
                Add to <ChevronDown size={12} />
              </button>
              {addToOpen && (
                <div
                  className="resolve-combine-add-to-menu"
                  role="menu"
                  data-testid="resolve-combine-add-to-menu"
                >
                  {buckets.map((bucket) => (
                    <button
                      key={bucket.id}
                      type="button"
                      role="menuitem"
                      data-testid="resolve-combine-add-to-option"
                      onClick={() => addSelectionTo(bucket.id)}
                    >
                      <span className="resolve-combine-add-to-name" title={bucket.canonical}>
                        {bucket.canonical}
                      </span>
                      <span className="muted">
                        {bucket.members.length} value{bucket.members.length === 1 ? '' : 's'}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </SelectionBar>

          {loading && (
            <div className="panel-empty" data-testid="resolve-combine-loading">
              Loading column values…
            </div>
          )}
          {loadError && !loading && (
            <div className="form-error resolve-combine-load-error" data-testid="resolve-combine-load-error">
              <span>{loadError}</span>
              <button
                type="button"
                className="btn"
                onClick={reload}
              >
                Retry
              </button>
            </div>
          )}

          {buckets.length > 0 && (
            <div className="resolve-combine-section">
              <div className="resolve-combine-section-label">
                Groups
                <span className="resolve-combine-section-count">{buckets.length}</span>
              </div>
              {duplicateNames.map(({ name, n }) => (
                <div
                  key={name}
                  className="resolve-combine-dup"
                  data-testid="resolve-combine-dup-nudge"
                  role="status"
                >
                  <AlertTriangle size={13} aria-hidden />
                  <span className="resolve-combine-dup-text">
                    {n === 2 ? 'Two' : n} groups are named{' '}
                    <strong>{name}</strong> — merge them?
                  </span>
                  <button
                    type="button"
                    className="btn resolve-combine-dup-merge"
                    data-testid="resolve-combine-dup-merge"
                    onClick={() => mergeDuplicates(name)}
                  >
                    Merge
                  </button>
                </div>
              ))}
              <div className="resolve-combine-buckets">
                {buckets.map((bucket) => (
                  <GroupCard
                    key={bucket.id}
                    testId="resolve-combine-bucket"
                    name={bucket.canonical}
                    defaultName={defaultNameFor(bucket)}
                    members={bucket.members.map((member) => ({
                      value: member,
                      count: countByValue.get(member) ?? 0,
                    }))}
                    collapsed={bucket.collapsed}
                    onCollapsedChange={(collapsed) => setCollapsed(bucket.id, collapsed)}
                    autoFocusName={bucket.id === freshBucketId}
                    onNameChange={(name) => renameBucket(bucket.id, name)}
                    onPromoteMember={(value) => renameBucket(bucket.id, value)}
                    onRemoveMember={(value) => removeMember(bucket.id, value)}
                    onDelete={() => deleteBucket(bucket.id)}
                  >
                    {renderAddValueBox(bucket)}
                  </GroupCard>
                ))}
              </div>
            </div>
          )}

          {preview && (
            <div className="resolve-combine-section">
              <div className="resolve-combine-section-label">
                Unassigned
                <span className="resolve-combine-section-count">
                  {unassignedEntries.length.toLocaleString()}
                </span>
              </div>
              {preview.truncated && (
                <div className="resolve-value-search-row resolve-combine-server-search">
                  <Search size={13} aria-hidden />
                  <input
                    className="form-input resolve-value-search"
                    data-testid="resolve-combine-unassigned-search"
                    value={unassignedQuery}
                    placeholder="Search all values"
                    aria-label="Search all column values"
                    onChange={(event) => {
                      setUnassignedQuery(event.target.value);
                      setServerQuery(event.target.value);
                    }}
                  />
                </div>
              )}
              <ValueList
                values={displayedUnassigned}
                selected={selected}
                onSelectionChange={setSelected}
                searchable={!preview.truncated}
                searchPlaceholder="Filter unassigned values"
                ariaLabel="Unassigned values"
                emptyText={
                  preview.truncated && unassignedQuery.trim() !== ''
                    ? `No values match "${unassignedQuery.trim()}"`
                    : 'Every value is grouped'
                }
                testId="resolve-combine-unassigned"
              />
              {searchStale && (
                <div
                  className="form-error resolve-combine-search-stale"
                  data-testid="resolve-combine-search-stale"
                  role="status"
                >
                  Column values changed since this list loaded — search results were
                  not added. Use “Reload values” to pick up the fresh values.
                </div>
              )}
              {preview.truncated && (
                <div className="form-hint resolve-combine-truncated" data-testid="resolve-combine-truncated">
                  Showing the {preview.values.length.toLocaleString()} most frequent of{' '}
                  {preview.distinct.toLocaleString()} distinct values. Searching checks
                  the whole column, and “Values not grouped” below decides what happens
                  to values you never group.
                </div>
              )}
              <p
                className="form-hint resolve-combine-exact-note"
                data-testid="resolve-combine-exact-note"
              >
                Values match exactly, including case and spaces.
              </p>
            </div>
          )}
      </div>

      {preview && (
        <label className="resolve-combine-setting" data-testid="resolve-combine-remainder-policy-row">
          <span className="resolve-combine-setting-label">Values not grouped</span>
          <PanelSelect
            className="form-input"
            data-testid="resolve-combine-remainder-policy"
            value={unmatched}
            onChange={(event) =>
              setUnmatched(
                event.target.value === 'null'
                  ? 'null'
                  : event.target.value === 'value'
                    ? 'value'
                    : 'keep',
              )}
          >
            <option value="keep">keep original</option>
            <option value="null">set null (clear them)</option>
            <option value="value">change to one value…</option>
          </PanelSelect>
        </label>
      )}
      {unmatched === 'value' && (
        <label className="resolve-combine-setting" data-testid="resolve-combine-remainder">
          <span className="resolve-combine-setting-label">Send remaining values to</span>
          <input
            className="form-input"
            data-testid="resolve-combine-remainder-input"
            value={unmatchedValue}
            placeholder="e.g. Other"
            onChange={(event) => setUnmatchedValue(event.target.value)}
          />
        </label>
      )}
      {summary && (
        <p className="run-scope-summary resolve-footer-summary"
          data-testid="resolve-footer-summary">{summary}</p>
      )}
    </div>
  );
}
