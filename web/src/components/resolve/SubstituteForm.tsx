// The bespoke drawer body for the "Substitute values" action. One row per
// distinct column value (value + count, ◆ marks the most common) with an inline
// editable "maps to" target; targets support an
// explicit (null); a "Values not listed" keep/null policy; "+ Add value" for
// values outside the enumeration; and a paste-map mode (Excel/Sheets TSV
// first, comma fallback) that populates the same editable table.
//
// The generated-action host owns this whole drawer's request lifecycle. This
// body keeps only presentation state and emits canonical Params. Enumeration
// comes from the read-only column-values preview (useColumnValuesPreview).
// Above ENUM_THRESHOLD distinct values the
// per-value table is NOT rendered from enumeration (nudge to paste a map /
// Cluster / Replace) but the paste-map mode keeps working — mapping keys then
// come from the pasted map, not the enumeration.
//
// State scoping: the outer component owns what survives a column switch
// (paste text and the unmatched policy); everything column-scoped
// lives in the body below, keyed on `${sheet.id}:${column}` so a switch
// remounts (and thereby resets) it.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { PanelSelect } from '../PanelSelect';
import type { GeneratedActionParamsBodyProps } from '../action-panel/GeneratedActionParamsBody';
import {
  useColumnValuesPreview,
} from './useColumnValuesPreview';
import './substitute-form.css';

export type SubstituteFormProps = GeneratedActionParamsBodyProps<'resolve.substitute'>;

/** Don't-enumerate threshold (contract: 150). Above this the row-per-value
 *  table would be a wall — the drawer nudges to a pasted map or another
 *  resolve action instead. */
const ENUM_THRESHOLD = 150;

/** One row's authored target. `isNull` wins over `text`; empty text with
 *  isNull false means "not mapped" — the row falls to the unmatched policy. */
interface TargetState {
  text: string;
  isNull: boolean;
}

const KEEP: TargetState = { text: '', isNull: false };

/** A row the user added beyond the enumeration ("+ Add value" or a pasted
 *  key the column page doesn't contain). Value is editable, key by id. */
interface ExtraRow {
  id: number;
  value: string;
  target: TargetState;
}

/** RFC-ish parse of a fully double-quoted two-field comma line —
 *  `"Acme, Inc.","Acme"` — with `""` as an escaped quote. Both fields must
 *  be quoted; the quoted content (inner commas, leading/trailing whitespace)
 *  is preserved EXACTLY. Returns null on mixed or malformed quoting
 *  (unterminated quote, an unquoted second field, trailing garbage, a third
 *  field) so the caller can count the line as skipped. */
function parseQuotedCommaLine(line: string): [string, string] | null {
  let i = 0;
  const skipSpaces = () => {
    while (i < line.length && line[i] === ' ') i += 1;
  };
  const readQuoted = (): string | null => {
    skipSpaces();
    if (line[i] !== '"') return null;
    i += 1;
    let out = '';
    while (i < line.length) {
      if (line[i] === '"') {
        if (line[i + 1] === '"') {
          out += '"';
          i += 2;
          continue;
        }
        i += 1;
        return out;
      }
      out += line[i];
      i += 1;
    }
    return null; // unterminated quote
  };
  const source = readQuoted();
  if (source === null) return null;
  skipSpaces();
  if (line[i] !== ',') return null;
  i += 1;
  const target = readQuoted();
  if (target === null) return null;
  skipSpaces();
  if (i !== line.length) return null;
  return [source, target];
}

/** Parse a pasted two-column map. Per line: TSV first (the Excel / Sheets
 *  clipboard default — cells kept EXACTLY, since mapping keys match cell
 *  values verbatim). Comma lines whose fields are BOTH double-quoted parse
 *  RFC-style — `"Acme, Inc.","Acme"` keeps its inner comma and any quoted
 *  whitespace exactly, `""` escapes a quote, and mixed/malformed quoting
 *  counts as a skipped line. Unquoted comma lines keep the first-comma split
 *  with both sides trimmed (hand-typed "M, male" almost always means "M").
 *  A target of the literal "(null)" authors an explicit null. Fully-blank
 *  lines are ignored silently; non-blank lines without both a source and a
 *  target count as skipped. Duplicate sources: last one wins, and each
 *  overwrite is counted so the report can say so instead of silently
 *  shrinking the total. (Module-private — exporting a non-component would
 *  trip react-refresh/only-export-components; the tests exercise it through
 *  the paste UI.) */
function parsePastedMap(raw: string): {
  entries: [string, string | null][];
  skipped: number;
  duplicates: number;
} {
  const entries = new Map<string, string | null>();
  let skipped = 0;
  let duplicates = 0;
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let source: string;
    let target: string;
    if (line.includes('\t')) {
      const cells = line.split('\t');
      source = cells[0];
      target = cells[1] ?? '';
    } else if (line.trimStart().startsWith('"')) {
      const quoted = parseQuotedCommaLine(line);
      if (quoted === null) {
        skipped += 1;
        continue;
      }
      [source, target] = quoted;
    } else {
      const idx = line.indexOf(',');
      if (idx === -1) {
        skipped += 1;
        continue;
      }
      source = line.slice(0, idx).trim();
      target = line.slice(idx + 1).trim();
    }
    if (!source || !target.trim()) {
      skipped += 1;
      continue;
    }
    if (entries.has(source)) duplicates += 1;
    entries.set(source, target === '(null)' ? null : target);
  }
  return { entries: [...entries.entries()], skipped, duplicates };
}

/** Inline target editor: a free text input ("maps to") plus an ∅ toggle for
 *  an explicit (null); the null state renders as a dashed chip with a clear
 *  affordance instead of the design's ▾ dropdown — same choices, one less
 *  popover. */
function TargetEditor({
  state,
  onChange,
}: {
  state: TargetState;
  onChange(next: TargetState): void;
}) {
  if (state.isNull) {
    return (
      <span className="resolve-substitute-null-chip" data-testid="resolve-substitute-null-chip">
        (null)
        <button
          type="button"
          className="resolve-substitute-null-clear"
          data-testid="resolve-substitute-null-clear"
          aria-label="Clear null target"
          title="Clear the (null) target"
          onClick={() => onChange({ text: '', isNull: false })}
        >
          <X size={11} />
        </button>
      </span>
    );
  }
  return (
    <>
      <input
        className="form-input resolve-substitute-target"
        data-testid="resolve-substitute-target-input"
        placeholder="keep as-is"
        aria-label="Maps to"
        value={state.text}
        onChange={(e) => onChange({ text: e.target.value, isNull: false })}
      />
      <button
        type="button"
        className="resolve-substitute-null-toggle"
        data-testid="resolve-substitute-null-toggle"
        aria-label="Set target to null"
        title="Map to (null) — blank the cell"
        onClick={() => onChange({ text: '', isNull: true })}
      >
        ∅
      </button>
    </>
  );
}

export function SubstituteForm(props: SubstituteFormProps) {
  if (!props.sheet) return <p className="form-hint">Choose a source sheet to configure this action.</p>;
  return <SubstituteFormForSheet {...props} sheet={props.sheet} />;
}

function SubstituteFormForSheet({
  sheet,
  params: canonical,
  setParams,
  Field,
}: SubstituteFormProps & { sheet: NonNullable<SubstituteFormProps['sheet']> }) {
  const canonicalMapping = canonical?.mapping
    && typeof canonical.mapping === 'object'
    && !Array.isArray(canonical.mapping)
    && Object.values(canonical.mapping).every(
      (value) => value === null || typeof value === 'string',
    )
    ? canonical.mapping as Record<string, string | null>
    : undefined;
  const canonicalUnmatched: 'keep' | 'null' = canonical?.unmatched === 'keep'
    || canonical?.unmatched === 'null'
    ? canonical.unmatched
    : 'keep';
  const saved = canonicalMapping
    ? {
        mapping: canonicalMapping,
        unmatched: canonicalUnmatched,
      }
    : undefined;
  const initialMapping = saved?.mapping;
  const initialColumn = typeof canonical.source === 'string'
    ? canonical.source
    : undefined;
  const activeColumn = typeof canonical.source === 'string' ? canonical.source : '';
  const paramsRef = useRef(canonical);
  useEffect(() => {
    paramsRef.current = canonical;
  }, [canonical]);
  const mergeParams = useCallback<SubstituteFormProps['setParams']>((owned) => {
    setParams({ ...paramsRef.current, ...owned });
  }, [setParams]);
  // Cross-column state — a column switch keeps these.
  const [pasteText, setPasteText] = useState('');
  const [unmatched, setUnmatched] = useState<'keep' | 'null'>(() => (
    saved?.unmatched ?? 'keep'
  ));
  return (
    <SubstituteFormBody
      key={`${sheet.id}:${activeColumn}`}
      sheet={sheet}
      activeColumn={activeColumn}
      setParams={mergeParams}
      Field={Field}
      pasteText={pasteText}
      setPasteText={setPasteText}
      unmatched={unmatched}
      setUnmatched={setUnmatched}
      initialMapping={activeColumn === initialColumn ? initialMapping : undefined}
    />
  );
}

interface SubstituteFormBodyProps {
  sheet: NonNullable<SubstituteFormProps['sheet']>;
  activeColumn: string;
  setParams: SubstituteFormProps['setParams'];
  Field: SubstituteFormProps['Field'];
  pasteText: string;
  setPasteText(text: string): void;
  unmatched: 'keep' | 'null';
  setUnmatched(next: 'keep' | 'null'): void;
  initialMapping?: Record<string, string | null>;
}

function SubstituteFormBody({
  sheet,
  activeColumn,
  setParams,
  Field,
  pasteText,
  setPasteText,
  unmatched,
  setUnmatched,
  initialMapping,
}: SubstituteFormBodyProps) {
  const [mode, setMode] = useState<'values' | 'paste'>('values');
  /** Targets for ENUMERATED values, keyed by exact cell value. A Map, not a
   *  plain object: cell values like "__proto__" / "constructor" / "toString"
   *  would collide with Object.prototype (inherited functions reading as
   *  truthy targets, `__proto__` assignment hitting the prototype setter). */
  const [targets, setTargets] = useState<Map<string, TargetState>>(new Map());
  const [extraRows, setExtraRows] = useState<ExtraRow[]>([]);
  const [pasteReport, setPasteReport] = useState<
    { applied: number; skipped: number; duplicates: number } | null
  >(null);
  const [pastedKeys, setPastedKeys] = useState<string[]>([]);
  const idRef = useRef(0);
  const hydratedInitialRef = useRef(false);
  // Preserve the saved mapping until the enumeration tells us which entries
  // belong in the main table. The body's initial params effect must not erase
  // it while that first request is in flight.
  const initialMappingRef = useRef(initialMapping);
  const [initialHydrationPending, setInitialHydrationPending] = useState(
    initialMapping !== undefined,
  );

  const { preview, loading, error, reloadNotice, reload } = useColumnValuesPreview({
    sheetId: sheet.id,
    column: activeColumn,
    droppedNoun: 'mapped value',
    // Over the threshold the enumeration table never renders — start the
    // user on the surface that works at that scale.
    onPreview: (out, source) => {
      if (out.distinct > ENUM_THRESHOLD) setMode('paste');
      const savedMapping = initialMappingRef.current;
      if (source === 'load' && savedMapping && !hydratedInitialRef.current) {
        hydratedInitialRef.current = true;
        const known = new Set<string>(
          out.distinct > ENUM_THRESHOLD ? [] : out.values.map((entry) => entry.value),
        );
        const targetState = (target: string | null): TargetState => ({
          text: target ?? '',
          isNull: target === null,
        });
        setTargets(new Map(
          Object.entries(savedMapping)
            .filter(([value]) => known.has(value))
            .map(([value, target]) => [value, targetState(target)]),
        ));
        setExtraRows(Object.entries(savedMapping)
          .filter(([value]) => !known.has(value))
          .map(([value, target]) => ({
            id: (idRef.current += 1),
            value,
            target: targetState(target),
          })));
        setInitialHydrationPending(false);
      }
    },
    // Reload keeps authored state; targets are re-keyed by value — values
    // still present keep their authored target, vanished ones are dropped
    // (counted for the notice), extra/pasted rows are kept as-is.
    pruneOnReload: (out) => {
      const known = new Set(out.values.map((v) => v.value));
      const dropped = [...targets].filter(
        ([value, t]) => (t.isNull || t.text.trim() !== '') && !known.has(value),
      ).length;
      setTargets((prev) =>
        new Map([...prev].filter(([value]) => known.has(value))),
      );
      return dropped;
    },
  });

  const overThreshold = preview !== null && preview.distinct > ENUM_THRESHOLD;
  const enumRows = useMemo(
    () => (preview !== null && preview.distinct <= ENUM_THRESHOLD ? preview.values : []),
    [preview],
  );
  const countByValue = useMemo(
    () => new Map((preview?.values ?? []).map((v) => [v.value, v.count])),
    [preview],
  );
  const enumValueSet = useMemo(() => new Set(enumRows.map((row) => row.value)), [enumRows]);
  // values arrive sorted count desc then value asc → the first row is the
  // most common (◆); ties resolve to the first, same as the sort.
  const topValue = enumRows.length > 0 ? enumRows[0].value : null;

  /** The effective mapping: null targets map to null; non-empty text maps to
   *  its trimmed target (the backend strips anyway — trimming here keeps the
   *  summary honest); empty targets are simply not in the mapping (those
   *  values fall to the unmatched policy). Extra rows override enumerated
   *  rows on key collision (they were authored later). The empty-string key
   *  is invalid on the wire and skipped. A Map (not a plain object) so keys
   *  like "__proto__" stay ordinary entries; the commit converts to the
   *  wire's plain-JSON object via Object.fromEntries, which defines own
   *  properties and never touches the prototype chain. */
  const mapping = useMemo(() => {
    const map = new Map<string, string | null>();
    const apply = (value: string, t: TargetState | undefined) => {
      if (!value || !t) return;
      if (t.isNull) map.set(value, null);
      else if (t.text.trim()) map.set(value, t.text.trim());
    };
    for (const [value, target] of targets) apply(value, target);
    for (const row of extraRows) apply(row.value, row.target);
    return map;
  }, [targets, extraRows]);

  const mappedCount = mapping.size;
  const targetCount = new Set(
    [...mapping.values()].map((t) => (t === null ? ' null' : `v:${t}`)),
  ).size;
  /** Rows the run will write. Under `keep` only mapped values change, so the
   *  figure is Σ loaded counts of the mapped keys — a floor whenever a
   *  mapped key sits beyond the loaded page. Under `null` the executor
   *  writes EVERY non-missing row (mapped → target, unmatched → null): that
   *  is exactly totalRows − missing from the UNFILTERED preview facts, exact
   *  regardless of paging, so no caveat applies. */
  const mappedRowSum = [...mapping.keys()].reduce(
    (sum, key) => sum + (countByValue.get(key) ?? 0),
    0,
  );
  const affectedRows =
    unmatched === 'null' && preview !== null
      ? preview.totalRows - preview.missing
      : mappedRowSum;
  /** True (keep policy only) when some mapped key is NOT among the loaded
   *  values while the column has more distinct values than the page — those
   *  keys contribute 0 to `affectedRows` even though they may match rows
   *  beyond the page, so the figure is a floor, not a total. */
  const affectedUnderstated =
    unmatched === 'keep' &&
    preview !== null &&
    preview.values.length < preview.distinct &&
    [...mapping.keys()].some((key) => !countByValue.has(key));
  /** How many rows share each resolved string target — powers the design's
   *  "↖ merges" hint on many-to-one rows. */
  const targetShareCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const t of mapping.values()) {
      if (t !== null) counts.set(t, (counts.get(t) ?? 0) + 1);
    }
    return counts;
  }, [mapping]);

  const setEnumTarget = (value: string, t: TargetState) =>
    setTargets((prev) => new Map(prev).set(value, t));
  const updateExtra = (id: number, patch: Partial<Omit<ExtraRow, 'id'>>) =>
    setExtraRows((prev) => prev.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  const removeExtra = (id: number) =>
    setExtraRows((prev) => prev.filter((row) => row.id !== id));
  const addExtra = () =>
    setExtraRows((prev) => [
      ...prev,
      { id: (idRef.current += 1), value: '', target: { ...KEEP } },
    ]);

  const applyPaste = () => {
    const { entries, skipped, duplicates } = parsePastedMap(pasteText);
    const known = new Set(enumRows.map((row) => row.value));
    // Pasted keys the enumeration table already shows land on those rows;
    // everything else becomes (or updates) an extra row.
    setTargets((prev) => {
      const next = new Map(prev);
      for (const [value, target] of entries) {
        if (known.has(value)) next.set(value, { text: target ?? '', isNull: target === null });
      }
      return next;
    });
    setExtraRows((prev) => {
      let next = [...prev];
      for (const [value, target] of entries) {
        if (known.has(value)) continue;
        const t: TargetState = { text: target ?? '', isNull: target === null };
        const existing = next.findIndex((row) => row.value === value);
        if (existing !== -1) {
          next = next.map((row, i) => (i === existing ? { ...row, target: t } : row));
        } else {
          next.push({ id: (idRef.current += 1), value, target: t });
        }
      }
      return next;
    });
    setPastedKeys(entries.map(([value]) => value));
    setPasteReport({ applied: entries.length, skipped, duplicates });
  };

  /** Coverage of the pasted map against what we KNOW about the column. With
   *  the full enumeration in hand it's exact; when the column has more
   *  distinct values than the fetched page, say so honestly instead of
   *  pretending. */
  const coverage = useMemo(() => {
    if (!pasteReport || !preview || pastedKeys.length === 0) return null;
    const known = new Set(preview.values.map((v) => v.value));
    const covered = pastedKeys.filter((key) => known.has(key)).length;
    if (preview.values.length >= preview.distinct) {
      const rest = preview.distinct - covered;
      const restNote = unmatched === 'null' ? `${rest} set to null (unmatched)` : `${rest} kept as-is`;
      return `Your map covers ${covered} of ${preview.distinct} distinct values · ${restNote}`;
    }
    return (
      `Your map covers ${covered} of the ${preview.values.length} loaded values — ` +
      `the column has ${preview.distinct.toLocaleString()} distinct, so coverage beyond ` +
      'the loaded page is not checked'
    );
  }, [pasteReport, pastedKeys, preview, unmatched]);

  const allMapped =
    preview !== null &&
    !overThreshold &&
    enumRows.length === preview.distinct &&
    enumRows.every((row) => mapping.has(row.value));

  let summary: string;
  if (mappedCount === 0) {
    summary = 'Nothing mapped yet — every value keeps its original.';
  } else {
    const head = allMapped && preview
      ? `All ${preview.distinct.toLocaleString()} values mapped`
      : preview && mappedCount <= preview.distinct
        ? `${mappedCount.toLocaleString()} of ${preview.distinct.toLocaleString()} values mapped`
        : `${mappedCount.toLocaleString()} values mapped`;
    const parts = [
      head,
      `${targetCount.toLocaleString()} ${targetCount === 1 ? 'target' : 'targets'}`,
      affectedUnderstated
        ? `affects at least ${affectedRows.toLocaleString()} rows (loaded values only)`
        : `affects ${affectedRows.toLocaleString()} rows`,
    ];
    if (unmatched === 'null') parts.push('unmatched → null');
    summary = parts.join(' · ');
  }

  useEffect(() => {
    if (initialHydrationPending) return;
    setParams({
      source: activeColumn,
      mapping: Object.fromEntries(mapping),
      unmatched,
    });
  }, [activeColumn, initialHydrationPending, mapping, setParams, unmatched]);

  const hasRows = enumRows.length > 0 || extraRows.length > 0;

  return (
    <div className="resolve-substitute-form" data-testid="resolve-substitute-form">
      <div className="resolve-substitute-scope">
        <Field name="source" testId="resolve-substitute-column" />
        {preview && (
          <span className="resolve-substitute-facts" data-testid="resolve-substitute-facts">
            {preview.distinct.toLocaleString()} distinct
            {preview.missing > 0 ? ` · ${preview.missing.toLocaleString()} empty` : ''}
          </span>
        )}
        {preview && (
          <button
            type="button"
            className="btn resolve-substitute-reload"
            data-testid="resolve-substitute-reload"
            disabled={loading}
            title="Re-fetch column values — keeps your mapping; use after the source changed under a preview"
            onClick={reload}
          >
            Reload values
          </button>
        )}
      </div>

      {reloadNotice && (
        <div
          className="form-hint resolve-substitute-reload-notice"
          data-testid="resolve-substitute-reload-notice"
          role="status"
        >
          {reloadNotice}
        </div>
      )}

      {loading && (
        <div className="form-hint" data-testid="resolve-substitute-loading">
          Loading column values…
        </div>
      )}
      {error && (
        <div className="form-error" data-testid="resolve-substitute-error">{error}</div>
      )}

      {overThreshold && preview && (
        <div
          className="form-hint resolve-substitute-threshold"
          data-testid="resolve-substitute-threshold-notice"
        >
          {preview.distinct.toLocaleString()} distinct values — too many to list row-by-row.
          Paste a map below, or try Cluster or Replace.
        </div>
      )}

      {preview && (
        <div className="resolve-substitute-modes">
          <span className="field-labels">Start from</span>
          <div className="resolve-substitute-mode-buttons" role="group" aria-label="Start from">
            <button
              type="button"
              data-testid="resolve-substitute-mode-values"
              className={mode === 'values' ? 'is-active' : ''}
              aria-pressed={mode === 'values'}
              disabled={overThreshold}
              title={overThreshold ? 'Too many distinct values to list row-by-row' : undefined}
              onClick={() => setMode('values')}
            >
              column values
            </button>
            <button
              type="button"
              data-testid="resolve-substitute-mode-paste"
              className={mode === 'paste' ? 'is-active' : ''}
              aria-pressed={mode === 'paste'}
              onClick={() => setMode('paste')}
            >
              pasted map
            </button>
          </div>
        </div>
      )}

      {mode === 'paste' && (
        <div className="resolve-substitute-paste-block">
          <textarea
            className="form-input resolve-substitute-paste"
            data-testid="resolve-substitute-paste"
            rows={5}
            aria-label="Pasted map"
            placeholder={'One mapping per line — paste two columns from Excel/Sheets, or value,target. Use (null) to blank a value.'}
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
          />
          <button
            type="button"
            className="btn resolve-substitute-paste-apply"
            data-testid="resolve-substitute-paste-apply"
            disabled={!pasteText.trim()}
            onClick={applyPaste}
          >
            Use map
          </button>
          {pasteReport && (
            <p className="form-hint" data-testid="resolve-substitute-paste-report">
              {`Parsed ${pasteReport.applied} mapping${pasteReport.applied === 1 ? '' : 's'}`}
              {pasteReport.duplicates > 0
                ? ` · ${pasteReport.duplicates} duplicate key${pasteReport.duplicates === 1 ? '' : 's'} (last kept)`
                : ''}
              {pasteReport.skipped > 0
                ? ` · ${pasteReport.skipped} line${pasteReport.skipped === 1 ? '' : 's'} skipped (blank or not two columns)`
                : ''}
            </p>
          )}
          {coverage && (
            <p className="form-hint" data-testid="resolve-substitute-coverage">{coverage}</p>
          )}
        </div>
      )}

      {hasRows && (
        <div className="resolve-substitute-table" data-testid="resolve-substitute-table">
          <div className="resolve-substitute-table-head">
            <span className="field-labels">Each value maps to</span>
            <span className="resolve-substitute-legend">◆ = most common</span>
          </div>
          <div className="resolve-substitute-rows">
            {enumRows.map((row) => {
              const mapped = mapping.get(row.value);
              const merges =
                typeof mapped === 'string' && (targetShareCounts.get(mapped) ?? 0) > 1;
              return (
                <div
                  className="resolve-substitute-row"
                  data-testid="resolve-substitute-row"
                  data-value={row.value}
                  key={`v:${row.value}`}
                >
                  <span className="resolve-substitute-value">
                    {row.value === topValue && (
                      <span
                        className="resolve-substitute-diamond"
                        data-testid="resolve-substitute-top-marker"
                        title="Most common value"
                        aria-label="Most common value"
                      >
                        ◆
                      </span>
                    )}
                    <span className="resolve-substitute-value-text" title={row.value}>
                      {row.value === '' ? '(empty)' : row.value}
                    </span>
                  </span>
                  <span className="resolve-substitute-count">{row.count.toLocaleString()}</span>
                  <span className="resolve-substitute-arrow" aria-hidden>→</span>
                  <div className="resolve-substitute-target-cell">
                    <TargetEditor
                      state={targets.get(row.value) ?? KEEP}
                      onChange={(t) => setEnumTarget(row.value, t)}
                    />
                    {merges && <span className="resolve-substitute-merge-hint">↖ merges</span>}
                  </div>
                </div>
              );
            })}
            {extraRows.map((row) => (
              <div
                className="resolve-substitute-row resolve-substitute-row-extra"
                data-testid="resolve-substitute-row"
                data-value={row.value}
                key={`x:${row.id}`}
              >
                <input
                  className="form-input resolve-substitute-value-input"
                  data-testid="resolve-substitute-value-input"
                  placeholder="value"
                  aria-label="Value"
                  value={row.value}
                  onChange={(e) => updateExtra(row.id, { value: e.target.value })}
                />
                {countByValue.has(row.value) ? (
                  <span className="resolve-substitute-count">
                    {(countByValue.get(row.value) ?? 0).toLocaleString()}
                  </span>
                ) : (
                  <span
                    className="resolve-substitute-count resolve-substitute-count-missing"
                    title="Not among the loaded column values — this row only applies if the column contains this exact value."
                  >
                    —
                    {row.value !== '' && (
                      <span
                        className="resolve-substitute-not-in-column"
                        data-testid="resolve-substitute-not-in-column"
                      >
                        not in column
                      </span>
                    )}
                  </span>
                )}
                <span className="resolve-substitute-arrow" aria-hidden>→</span>
                <div className="resolve-substitute-target-cell">
                  <TargetEditor
                    state={row.target}
                    onChange={(t) => updateExtra(row.id, { target: t })}
                  />
                  {enumValueSet.has(row.value) &&
                    (row.target.isNull || row.target.text.trim() !== '') && (
                      <span
                        className="resolve-substitute-override"
                        data-testid="resolve-substitute-override-warning"
                        title="Same value as an enumerated row — this row's target wins."
                      >
                        overrides the row above
                      </span>
                    )}
                </div>
                <button
                  type="button"
                  className="icon-btn resolve-substitute-row-remove"
                  data-testid="resolve-substitute-row-remove"
                  aria-label="Remove value"
                  title="Remove this value"
                  onClick={() => removeExtra(row.id)}
                >
                  <X size={12} />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {preview && (
        <p
          className="form-hint resolve-substitute-exact-note"
          data-testid="resolve-substitute-exact-note"
        >
          Values match exactly, including case and spaces.
        </p>
      )}

      {preview && !loading && (
        <button
          type="button"
          className="btn resolve-substitute-add"
          data-testid="resolve-substitute-add-value"
          onClick={addExtra}
        >
          <Plus size={13} /> Add value
        </button>
      )}

      {preview && (
        <label className="resolve-substitute-policy-row">
          <span className="field-labels">Values not listed</span>
          <PanelSelect
            className="form-input"
            data-testid="resolve-substitute-policy"
            value={unmatched}
            onChange={(e) => setUnmatched(e.target.value === 'null' ? 'null' : 'keep')}
          >
            <option value="keep">keep original</option>
            <option value="null">set null</option>
          </PanelSelect>
        </label>
      )}

      <p className="run-scope-summary resolve-footer-summary"
        data-testid="resolve-footer-summary">{summary}</p>
    </div>
  );
}
