// The bespoke drawer body for "Replace by rules". An ordered first-match-wins
// rules list (contains / exact / regex → whole-cell set-to, null allowed), a
// no-match policy, and per-rule match counts.
//
// CRITICAL INVARIANT — server-side evaluation ONLY: every count comes from
// api.replaceRulesPreview, which runs the SAME Python `re` engine the commit
// executor uses. No rule is ever evaluated with a JS RegExp here, so the
// authoring UI can never disagree with Apply.
//
// The body emits canonical Params. The generated-action host owns request
// identity, validation, output naming, preview, and execution.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Plus, X } from 'lucide-react';
import { PanelSelect } from '../PanelSelect';
import {
  ApiError,
} from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import type { GeneratedActionParamsBodyProps } from '../action-panel/GeneratedActionParamsBody';
import './replace-form.css';

export type ReplaceFormProps = GeneratedActionParamsBodyProps<'resolve.replace'>;

type MatchKind = 'contains' | 'exact' | 'regex';

/** One authored rule row. `target: null` = write a null cell on match. */
interface RuleRow {
  id: number;
  match: MatchKind;
  pattern: string;
  target: string | null;
  caseSensitive: boolean;
}

/** The shape of one rule AS SENT to the preview endpoint — kept per response
 *  so freshness (dim-on-edit) compares against what the counts were actually
 *  computed for, never live state. */
interface SentRule {
  id: number;
  match: MatchKind;
  pattern: string;
  target: string | null;
  caseSensitive: boolean;
}

/** Immutable snapshot of the latest landed preview. Counts are keyed by rule
 *  id (stable across reorders/edits) so an edited rule can keep showing its
 *  last count, dimmed, instead of flickering empty. */
interface PreviewSnapshot {
  sent: SentRule[];
  countsById: ReadonlyMap<number, { matchedRows: number; matchedValues: number }>;
  /** Σ ruleCounts[].matchedRows — rules are first-match-wins so per-rule
   *  counts are disjoint and their sum is the true matched-row total.
   *  (totalRows also counts cells the server classifies as missing, which
   *  appear in NEITHER ruleCounts nor unmatchedRows — so totalRows −
   *  unmatchedRows would overcount matches by the number of missing cells.) */
  matchedRows: number;
  totalRows: number;
  unmatchedRows: number;
  unmatched: 'keep' | 'null';
}

const MATCH_OPTIONS: { value: MatchKind; label: string }[] = [
  { value: 'contains', label: 'contains' },
  { value: 'exact', label: 'exact' },
  { value: 'regex', label: 'regex' },
];

const PATTERN_PLACEHOLDER: Record<MatchKind, string> = {
  contains: 'text to find',
  exact: 'whole cell value',
  regex: 'regular expression',
};

/** Matches the schema cap (contracts MAX_REPLACE_RULES). */
const MAX_RULES = 200;
const PREVIEW_DEBOUNCE_MS = 350;

const fmt = (n: number): string => n.toLocaleString();
const plural = (n: number, one: string, many: string): string => (n === 1 ? one : many);
const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/** Preview eligibility: blank patterns are authoring noise and blank (non-
 *  null) targets are rejected by the ReplaceRule contract, so both are held
 *  out of preview calls; a rule whose regex the server refused stays out
 *  until its pattern text changes (otherwise every preview keeps failing). */
function ruleBlocker(rule: RuleRow, regexError: RegexFlag | undefined): string | null {
  if (rule.pattern === '') return 'needs a pattern';
  if (rule.target !== null && rule.target.trim() === '') return 'needs a target';
  if (regexError && rule.match === 'regex' && regexError.pattern === rule.pattern) {
    return 'invalid regex';
  }
  return null;
}

/** A server invalid_regex verdict, pinned to the exact pattern text it was
 *  issued for — editing the pattern clears it, retyping it restores it. */
interface RegexFlag {
  pattern: string;
  message: string;
}

function sameEval(a: SentRule, b: SentRule): boolean {
  return (
    a.id === b.id &&
    a.match === b.match &&
    a.pattern === b.pattern &&
    a.caseSensitive === b.caseSensitive
  );
}

export function ReplaceForm(props: ReplaceFormProps) {
  if (!props.sheet) return <p className="form-hint">Choose a source sheet to configure this action.</p>;
  return <ReplaceFormForSheet {...props} sheet={props.sheet} />;
}

function ReplaceFormForSheet({
  sheet,
  params: canonical,
  setParams,
  Field,
}: ReplaceFormProps & { sheet: NonNullable<ReplaceFormProps['sheet']> }) {
  const { projectApi: api } = useWorkspaceStores();
  const canonicalRuleCount = Array.isArray(canonical?.rules)
    ? canonical.rules.length
    : undefined;
  const canonicalRules = Array.isArray(canonical?.rules)
    ? canonical.rules.flatMap((rule) => {
        if (
          rule === null
          || typeof rule !== 'object'
          || Array.isArray(rule)
        ) return [];
        const value = rule as Record<string, unknown>;
        if (
          value.match !== 'contains'
          && value.match !== 'exact'
          && value.match !== 'regex'
        ) return [];
        if (
          typeof value.pattern !== 'string'
          || (value.target !== null && typeof value.target !== 'string')
        ) return [];
        return [{
          match: value.match,
          pattern: value.pattern,
          target: value.target,
          caseSensitive: value.case_sensitive === true,
        } satisfies Omit<RuleRow, 'id'>];
      })
    : undefined;
  const canonicalUnmatched: 'keep' | 'null' = canonical.unmatched === 'null'
    ? 'null'
    : 'keep';
  const saved = canonicalRules
    && canonicalRules.length === canonicalRuleCount
    ? {
        rules: canonicalRules,
        unmatched: canonicalUnmatched,
      }
    : undefined;
  const activeColumn = typeof canonical.source === 'string' ? canonical.source : '';
  const paramsRef = useRef(canonical);
  useEffect(() => {
    paramsRef.current = canonical;
  }, [canonical]);
  const mergeParams = useCallback<ReplaceFormProps['setParams']>((owned) => {
    setParams({ ...paramsRef.current, ...owned });
  }, [setParams]);
  // Rule ids: 1 is the starter row baked into the initial state below; the
  // counter is only ever advanced from event handlers (never during render).
  const idRef = useRef(Math.max(saved?.rules.length ?? 0, 1));
  const nextId = () => ++idRef.current;
  // A saved rule list retains exact order and stable per-row identity; a
  // fresh drawer opens with one blank starter row.
  const [rules, setRules] = useState<RuleRow[]>(() => saved?.rules.length
    ? saved.rules.map((rule, index) => ({ id: index + 1, ...rule }))
    : [{ id: 1, match: 'contains', pattern: '', target: '', caseSensitive: false }]);
  const [unmatched, setUnmatched] = useState<'keep' | 'null'>(saved?.unmatched ?? 'keep');
  const [preview, setPreview] = useState<PreviewSnapshot | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  // Bumped by the preview-error Retry button to re-arm the debounce effect
  // with an otherwise-identical payload.
  const [retryNonce, setRetryNonce] = useState(0);
  const [regexErrors, setRegexErrors] = useState<Record<number, RegexFlag>>({});
  // Monotonic request id: a response (or error) whose id is stale by the time
  // it resolves is discarded (SearchPanel's seq-guard pattern).
  const seqRef = useRef(0);

  const includedRules = useMemo(
    () => rules.filter((rule) => ruleBlocker(rule, regexErrors[rule.id]) === null),
    [rules, regexErrors],
  );

  // The exact payload the next preview will send (memoized so the debounce
  // effect re-arms only when something the server would see has changed).
  const previewPayload = useMemo(
    () => ({
      sheetId: sheet.id,
      inputColumn: activeColumn,
      sent: includedRules.map<SentRule>((rule) => ({
        id: rule.id,
        match: rule.match,
        pattern: rule.pattern,
        target: rule.target,
        caseSensitive: rule.caseSensitive,
      })),
      unmatched,
      retryNonce,
    }),
    [sheet.id, activeColumn, includedRules, unmatched, retryNonce],
  );

  useEffect(() => {
    if (!previewPayload.inputColumn) return;
    const mine = ++seqRef.current;
    const { sheetId, inputColumn, sent, unmatched: sentUnmatched } = previewPayload;
    const timer = setTimeout(() => {
      api
        .replaceRulesPreview({
          sheetId,
          inputColumn,
          rules: sent.map((rule) => ({
            match: rule.match,
            pattern: rule.pattern,
            target: rule.target,
            case_sensitive: rule.caseSensitive,
          })),
          unmatched: sentUnmatched,
        })
        .then((out) => {
          if (seqRef.current !== mine) return; // superseded — discard
          setPreview({
            sent,
            countsById: new Map(
              out.ruleCounts
                .filter((count) => sent[count.index] !== undefined)
                .map((count) => [
                  sent[count.index].id,
                  { matchedRows: count.matchedRows, matchedValues: count.matchedValues },
                ]),
            ),
            matchedRows: out.ruleCounts.reduce((sum, count) => sum + count.matchedRows, 0),
            totalRows: out.totalRows,
            unmatchedRows: out.unmatchedRows,
            unmatched: sentUnmatched,
          });
          setPreviewError(null);
        })
        .catch((e: unknown) => {
          if (seqRef.current !== mine) return; // discard the stale error too
          if (e instanceof ApiError && e.code === 'invalid_regex') {
            // Attribute to the offending rule row. The server stops at the
            // FIRST invalid rule and echoes its pattern in details, so the
            // first sent regex rule with that pattern is the culprit. (The
            // wire error's `field` index isn't surfaced through ApiError —
            // pattern matching is the available anchor.)
            const detailPattern =
              typeof e.details?.pattern === 'string' ? e.details.pattern : null;
            const culprit = sent.find(
              (rule) =>
                rule.match === 'regex' &&
                (detailPattern === null || rule.pattern === detailPattern),
            );
            if (culprit) {
              setRegexErrors((prev) => ({
                ...prev,
                [culprit.id]: { pattern: culprit.pattern, message: e.message },
              }));
              setPreviewError(null);
              return;
            }
          }
          setPreviewError(msg(e));
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [api, previewPayload]);

  // Freshness: with first-match-wins, editing/reordering a rule invalidates
  // every count from that position DOWN, but leaves earlier rules' counts
  // exact. Counts for included-list positions < freshUpTo render solid;
  // everything past the divergence point dims.
  const freshUpTo = useMemo(() => {
    if (!preview) return 0;
    const current = previewPayload.sent;
    let n = 0;
    while (
      n < current.length &&
      n < preview.sent.length &&
      sameEval(current[n], preview.sent[n])
    ) {
      n += 1;
    }
    return n;
  }, [preview, previewPayload]);
  const rulesFresh =
    preview !== null &&
    freshUpTo === previewPayload.sent.length &&
    preview.sent.length === previewPayload.sent.length;

  const updateRule = (id: number, patch: Partial<Omit<RuleRow, 'id'>>) => {
    setRules((prev) => prev.map((rule) => (rule.id === id ? { ...rule, ...patch } : rule)));
  };
  const deleteRule = (id: number) => {
    setRules((prev) => prev.filter((rule) => rule.id !== id));
    setRegexErrors((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  };
  const moveRule = (index: number, delta: -1 | 1) => {
    setRules((prev) => {
      const to = index + delta;
      if (to < 0 || to >= prev.length) return prev;
      const next = [...prev];
      const [row] = next.splice(index, 1);
      next.splice(to, 0, row);
      return next;
    });
  };
  const addRule = () => {
    setRules((prev) =>
      prev.length >= MAX_RULES
        ? prev
        : [...prev, { id: nextId(), match: 'contains', pattern: '', target: '', caseSensitive: false }],
    );
  };
  useEffect(() => {
    mergeParams({
      source: activeColumn,
      rules: rules.map((rule) => ({
        match: rule.match,
        pattern: rule.pattern,
        target: rule.target,
        case_sensitive: rule.caseSensitive,
      })),
      unmatched,
    });
  }, [activeColumn, mergeParams, rules, unmatched]);

  // ── Footer summary ────────────────────────────────────────────────────
  const summary = useMemo(() => {
    if (!preview) return undefined;
    const keptLabel = preview.unmatched === 'keep' ? 'kept as-is' : 'set to null';
    // All three numbers derive from response fields (never re-guessed
    // client-side): matched = Σ ruleCounts, kept = unmatchedRows, and the
    // residual is whatever the server classified as missing — so the footer
    // stays honest regardless of how the server defines "missing".
    const missingRows = preview.totalRows - preview.matchedRows - preview.unmatchedRows;
    const text =
      preview.sent.length === 0
        ? `No rules yet · ${fmt(preview.totalRows)} rows ${keptLabel}`
        : `${preview.sent.length} ${plural(preview.sent.length, 'rule matches', 'rules match')} ` +
          `${fmt(preview.matchedRows)} of ${fmt(preview.totalRows)} rows · ` +
          `${fmt(preview.unmatchedRows)} ${keptLabel}` +
          (missingRows > 0 ? ` · ${fmt(missingRows)} empty untouched` : '');
    return (
      <span className={rulesFresh ? undefined : 'resolve-replace-stale'}>{text}</span>
    );
  }, [preview, rulesFresh]);

  return (
    <div className="resolve-replace-form" data-testid="resolve-replace-form">
      <div className="resolve-replace-scope">
        <div className="field-group resolve-replace-column-field">
          <Field name="source" testId="resolve-replace-column" />
          <span className="form-hint resolve-replace-order-hint">Rules run top to bottom — first match wins.</span>
        </div>
      </div>

      {!activeColumn && (
        <p className="form-hint">Replace needs a text column on this sheet.</p>
      )}

      <div className="resolve-replace-rules">
        {rules.map((rule, index) => {
          const regexFlag = regexErrors[rule.id];
          const blocker = ruleBlocker(rule, regexFlag);
          const invalidRegex = blocker === 'invalid regex';
          const count = preview?.countsById.get(rule.id);
          const includedPos = includedRules.findIndex((r) => r.id === rule.id);
          const countFresh =
            rulesFresh || (includedPos >= 0 && includedPos < freshUpTo);
          return (
            <div className="resolve-replace-rule" data-testid="resolve-replace-rule" key={rule.id}>
              <div className="resolve-replace-rule-main">
                <span className="resolve-replace-rule-index">{index + 1}</span>
                <PanelSelect
                  className="form-input resolve-replace-rule-kind"
                  data-testid="resolve-replace-rule-match"
                  aria-label="Match kind"
                  value={rule.match}
                  onChange={(e) => updateRule(rule.id, { match: e.target.value as MatchKind })}
                >
                  {MATCH_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </PanelSelect>
                <input
                  className="form-input resolve-replace-rule-pattern"
                  data-testid="resolve-replace-rule-pattern"
                  aria-label="Pattern"
                  aria-invalid={invalidRegex}
                  placeholder={PATTERN_PLACEHOLDER[rule.match]}
                  value={rule.pattern}
                  onChange={(e) => updateRule(rule.id, { pattern: e.target.value })}
                />
                <button
                  type="button"
                  className={`icon-btn resolve-replace-case${rule.caseSensitive ? ' is-on' : ''}`}
                  data-testid="resolve-replace-rule-case"
                  aria-pressed={rule.caseSensitive}
                  aria-label="Match case"
                  title={rule.caseSensitive ? 'Case-sensitive — click to ignore case' : 'Ignoring case — click to match case'}
                  onClick={() => updateRule(rule.id, { caseSensitive: !rule.caseSensitive })}
                >
                  Aa
                </button>
                <span className="resolve-replace-rule-tools">
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="resolve-replace-rule-up"
                    aria-label="Move rule up"
                    title="Move rule up"
                    disabled={index === 0}
                    onClick={() => moveRule(index, -1)}
                  >
                    <ArrowUp size={12} />
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="resolve-replace-rule-down"
                    aria-label="Move rule down"
                    title="Move rule down"
                    disabled={index === rules.length - 1}
                    onClick={() => moveRule(index, 1)}
                  >
                    <ArrowDown size={12} />
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="resolve-replace-rule-delete"
                    aria-label="Delete rule"
                    title="Delete rule"
                    onClick={() => deleteRule(rule.id)}
                  >
                    <X size={13} />
                  </button>
                </span>
              </div>
              <div className="resolve-replace-rule-target-row">
                <span className="resolve-replace-arrow" aria-hidden="true">→</span>
                <span className="resolve-replace-set-to">set to</span>
                {rule.target === null ? (
                  <button
                    type="button"
                    className="resolve-replace-null-chip"
                    data-testid="resolve-replace-rule-target-null"
                    title="Matching cells become null — click to type a value instead"
                    onClick={() => updateRule(rule.id, { target: '' })}
                  >
                    (null)
                  </button>
                ) : (
                  <input
                    className="form-input resolve-replace-rule-target"
                    data-testid="resolve-replace-rule-target"
                    aria-label="Replacement value"
                    placeholder="replacement value"
                    value={rule.target}
                    onChange={(e) => updateRule(rule.id, { target: e.target.value })}
                  />
                )}
                <button
                  type="button"
                  className={`icon-btn resolve-replace-null-toggle${rule.target === null ? ' is-on' : ''}`}
                  data-testid="resolve-replace-rule-null"
                  aria-pressed={rule.target === null}
                  aria-label="Set to null on match"
                  title="Set matching cells to null"
                  onClick={() =>
                    updateRule(rule.id, { target: rule.target === null ? '' : null })
                  }
                >
                  ∅
                </button>
              </div>
              {(regexFlag && invalidRegex) || blocker || count ? (
                <div className="resolve-replace-rule-meta">
                  {invalidRegex && regexFlag ? (
                    <span className="form-error" data-testid="resolve-replace-rule-error">
                      {regexFlag.message}
                    </span>
                  ) : blocker ? (
                    <span className="resolve-replace-rule-note" data-testid="resolve-replace-rule-skipped">
                      {blocker === 'needs a pattern'
                        ? 'add a pattern — this rule is skipped'
                        : 'add a target (or ∅ for null) — this rule is skipped'}
                    </span>
                  ) : count ? (
                    <span
                      className={`resolve-replace-rule-count${countFresh ? '' : ' resolve-replace-stale'}`}
                      data-testid="resolve-replace-rule-count"
                    >
                      catches {fmt(count.matchedRows)} {plural(count.matchedRows, 'row', 'rows')}
                    </span>
                  ) : null}
                </div>
              ) : null}
            </div>
          );
        })}
        <button
          type="button"
          className="btn resolve-replace-add-rule"
          data-testid="resolve-replace-add-rule"
          disabled={rules.length >= MAX_RULES}
          onClick={addRule}
        >
          <Plus size={13} /> Add rule
        </button>
      </div>

      {previewError && (
        <div className="form-error" data-testid="resolve-replace-preview-error">
          {previewError}{' '}
          <button
            type="button"
            className="btn resolve-replace-preview-retry"
            data-testid="resolve-replace-preview-retry"
            onClick={() => setRetryNonce((nonce) => nonce + 1)}
          >
            Retry preview
          </button>
        </div>
      )}

      <label className="resolve-replace-policy-row">
        <span className="field-labels">When no rule matches</span>
        <PanelSelect
          className="form-input resolve-replace-policy"
          data-testid="resolve-replace-policy"
          value={unmatched}
          onChange={(e) => setUnmatched(e.target.value as 'keep' | 'null')}
        >
          <option value="keep">keep original</option>
          <option value="null">set null</option>
        </PanelSelect>
      </label>

      {summary && (
        <p className="run-scope-summary resolve-footer-summary"
          data-testid="resolve-footer-summary">{summary}</p>
      )}
    </div>
  );
}
