import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { EvaluationApi, EvaluationCell, EvaluationResponse, EvaluationRule } from '../../api/evaluation';
import type { GeneratedActionDraft, JsonValue } from '../../api/types';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { AnchoredMenu } from '../../components/MenuPop';
import { PanelSelect } from '../../components/PanelSelect';
import type { ActionCompareGridProps } from '../ActionCompareGrid';
import type { ActionCompareVariantEditorRenderProps } from '../ActionCompareTab';
import type { ActionCompareSession, ActionCompareSessionSnapshot, ActionCompareUseSettings } from '../actionCompareSession';
import { matchAnswer } from '../evaluationMatching';
import { ComparisonReview } from './ComparisonReview';
import { ComparisonSources } from './ComparisonSources';
import { ComparisonResults, ManageShelf, MatchRuleDialog } from './ComparisonPanels';
import { answerText, disagreementRows, expectedValueFromText, filteredEvaluationRows, groupedAnswers, MATCH_RULES, type EvaluationRowFilter } from './comparisonEvaluation';
import { useComparisonEvaluation } from './useComparisonEvaluation';
import { TrySeveral } from './TrySeveral';
import { FocusedAnswerValue } from './FocusedAnswerValue';
import { ComparisonAnswerValue } from './ComparisonAnswerValue';
import { UnavailableExpectedLinks } from '../../workspace/UnavailableExpectedLinks';
import { isExpectedLinkAvailable } from '../../workspace/expectedLinkAvailability';
import './evaluation.css';

export interface ComparisonEvaluationWorkspaceProps {
  api: EvaluationApi; state: ActionCompareSessionSnapshot; session: ActionCompareSession;
  initialLens?: 'each_other' | 'expected'; onJoinExpected?(): void; onExpectedAnswersChanged?(): void; onShelved?(): void;
  modelField?: string | null; instructionsField?: string | null;
  renderVariantEditor?(props: ActionCompareVariantEditorRenderProps): ReactNode;
  rowScope?: 'sample' | 'all' | 'expected'; rowScopePending?: boolean;
  rowScopeTotal?: number | null;
  dataRevision?: number;
  onChangeRowScope?(scope: 'sample' | 'all' | 'expected', lens?: 'each_other' | 'expected'): void;
  onOpenVariant(id: string): void; onUseSettings(selection: ActionCompareUseSettings): void;
  renderGrid(evaluation: NonNullable<ActionCompareGridProps['evaluation']>): ReactNode;
}

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function ComparisonEvaluationWorkspace(p: ComparisonEvaluationWorkspaceProps) {
  const e = useComparisonEvaluation(p.api, p.state, p.dataRevision);
  const { api, onExpectedAnswersChanged } = p;
  const { mutate, setLink, request: evaluationRequest, setError: setEvaluationError } = e;
  const basis = p.state.context.comparison_basis;
  const linkRef = useRef(e.link);
  useLayoutEffect(() => { linkRef.current = e.link; }, [e.link]);
  const [lens, setLens] = useState<'each_other' | 'expected'>(p.initialLens ?? 'each_other');
  const [field, setField] = useState(p.state.context.output_columns[0]?.logical_key ?? '');
  const [filter, setFilter] = useState<EvaluationRowFilter>('all');
  const [menu, setMenu] = useState(false);
  const menuAnchor = useRef<HTMLButtonElement>(null);
  const [ruleOpen, setRuleOpen] = useState(false);
  const [resultsOpen, setResultsOpen] = useState(false);
  const [shelfOpen, setShelfOpen] = useState(false);
  const [severalOpen, setSeveralOpen] = useState(false);
  const [queue, setQueue] = useState<{ mode: 'inspect' | 'score' | 'disagreements'; rowId: number; filter: 'all' | 'misses' | 'to_score'; variant?: string } | null>(null);
  const [draft, setDraft] = useState<{ rowId: number; field: string; value: string } | null>(null);
  const [rejected, setRejected] = useState<Set<string>>(new Set());
  const [conflict, setConflict] = useState<{ value: JsonValue; looser: EvaluationRule | null } | null>(null);
  const [unlinkedRules, setUnlinkedRules] = useState<Record<string, EvaluationRule>>({});
  const [fullCells, setFullCells] = useState<{ key: string; response: EvaluationResponse | null; cells: Record<string, EvaluationCell>; expected?: EvaluationCell }>({ key: '', response: null, cells: {} });
  const pending = useRef<{ rowId: number; field: string; value: JsonValue; rule: EvaluationRule; version: number } | null>(null);
  const writeVersion = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const expectedSave = useRef<Promise<boolean>>(Promise.resolve(true));
  const running = p.state.sequenceStatus === 'running';
  const response = e.response;
  const currentField = p.state.context.output_columns.find((column) => column.logical_key === field);
  const expectedField = e.link?.fields.find((item) => item.logical_key === field);
  const sourceUnavailable = e.link !== null && !isExpectedLinkAvailable(e.link);
  const rule = expectedField?.rule ?? unlinkedRules[field] ?? 'exact';
  const rows = response && lens === 'expected' ? filteredEvaluationRows(response, field, filter) : p.state.context.sample_row_ids;
  const disagreements = response ? disagreementRows(response, field) : [];
  const unscored = response ? response.variants.reduce((total, variant) => total + (variant.scores[field]?.remaining ?? 0), 0) : 0;
  const modelField = p.modelField ?? ['model', 'engine'].find((name) => name in p.state.context.draft.params);
  const instructionsField = p.instructionsField ?? ['instruction', 'instructions', 'extra_instructions', 'prompt', 'question', 'context'].find((name) => typeof p.state.context.draft.params[name] === 'string');

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const saveExpected = useCallback(async (write: { rowId: number; field: string; value: JsonValue; rule: EvaluationRule }) => {
    if (!linkRef.current && (write.value === null || write.value === '')) return true;
    const result = await mutate(() => api.saveExpectedAnswer({ basis,
      link_id: linkRef.current?.id ?? null, row_id: write.rowId, logical_key: write.field, value: write.value, rule: write.rule }));
    if (result) { linkRef.current = result; setLink(result); onExpectedAnswersChanged?.(); }
    return Boolean(result);
  }, [mutate, api, basis, setLink, onExpectedAnswersChanged]);

  const flushExpected = useCallback(async () => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    const write = pending.current; pending.current = null;
    if (!write) return expectedSave.current;
    const saving = expectedSave.current.then(() => saveExpected(write));
    expectedSave.current = saving;
    if (!await saving) { if (!pending.current && write.version === writeVersion.current) pending.current = write; return false; }
    return true;
  }, [saveExpected]);
  const flushOnLeave = useRef(flushExpected);
  useLayoutEffect(() => { flushOnLeave.current = flushExpected; }, [flushExpected]);
  useEffect(() => () => { void flushOnLeave.current(); }, []);

  const stageExpected = (write: { rowId: number; field: string; value: JsonValue; rule: EvaluationRule }) => {
    pending.current = { ...write, version: ++writeVersion.current };
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
  };

  const expectedChanged = (value: string) => {
    if (!queue || queueRow === undefined) return;
    setDraft({ rowId: queueRow, field, value });
    stageExpected({ rowId: queueRow, field,
      value: expectedValueFromText(value, response?.expected[String(queueRow)]?.[field], currentField?.column_type ?? 'text'), rule });
    timer.current = setTimeout(() => { void flushExpected(); }, 400);
  };
  const chooseExpected = async (value: JsonValue) => {
    if (queueRow === undefined) return false;
    setDraft({ rowId: queueRow, field, value: answerText(value) });
    stageExpected({ rowId: queueRow, field, value, rule });
    return flushExpected();
  };

  const changeRule = async (next: EvaluationRule) => {
    if (!await flushExpected()) return;
    const target = linkRef.current?.fields.find((item) => item.logical_key === field);
    if (!target) { setUnlinkedRules((current) => ({ ...current, [field]: next })); return; }
    const result = await e.mutate(() => p.api.setRule(target.expected_column_id, next));
    if (result) p.onExpectedAnswersChanged?.();
  };

  const queueRows = useMemo(() => {
    if (!queue || !response) return [];
    if (queue.mode === 'disagreements') return disagreementRows(response, field);
    if (queue.filter === 'all') return rows;
    return response.row_ids.filter((rowId) => response.variants.some((variant) => (!queue.variant || queue.variant === variant.key)
      && variant.verdicts[String(rowId)]?.[field] === (queue.filter === 'to_score' ? 'unscored' : 'miss')));
  }, [queue, response, field, rows]);
  const queueRow = queue && queueRows.includes(queue.rowId) ? queue.rowId : queueRows[0];
  const focusedKey = JSON.stringify([e.request, queueRow, field]);
  const loaded = fullCells.key === focusedKey && fullCells.response === response ? fullCells : null;
  const boundedExpected = queueRow === undefined ? undefined : response?.expected_bounded?.[String(queueRow)]?.[field];
  const storedExpected = loaded?.expected?.value ?? (queueRow === undefined ? undefined : response?.expected[String(queueRow)]?.[field]);
  const activeDraft = draft && draft.rowId === queueRow && draft.field === field ? draft : null;
  const expectedText = activeDraft?.value ?? answerText(storedExpected);
  const expectedValue = expectedValueFromText(expectedText, storedExpected, currentField?.column_type ?? 'text');
  const queueActive = queue !== null;
  useEffect(() => {
    if (!response || queueRow === undefined || !queueActive) return;
    let alive = true;
    const groups = groupedAnswers(response, queueRow, field);
    const bounded = groups.filter((group) => group.bounded);
    const expected = response.expected_bounded?.[String(queueRow)]?.[field];
    if (!bounded.length && !expected) return;
    void Promise.all([
      ...bounded.map(async (group) => [group.key, await api.getCell({ comparison: evaluationRequest, variant_key: group.eligibleVariantKeys[0] ?? group.variantKeys[0], row_id: queueRow, logical_key: field })] as const),
      ...(expected ? [(async () => ['expected', await api.getCell({ comparison: evaluationRequest, variant_key: 'expected', row_id: queueRow, logical_key: field })] as const)()] : []),
    ]).then((entries) => {
      if (alive) setFullCells({ key: focusedKey, response, cells: Object.fromEntries(entries), expected: entries.find(([key]) => key === 'expected')?.[1] });
    }, (problem) => { if (alive) setEvaluationError(problem instanceof Error ? problem.message : String(problem)); });
    return () => { alive = false; };
  }, [api, response, queueRow, field, focusedKey, evaluationRequest, queueActive, setEvaluationError]);

  const groups = response && queueRow !== undefined ? groupedAnswers(response, queueRow, field).map((group) => {
    const cell = loaded?.cells[group.key];
    return cell ? { ...group, value: cell.value, answerText: answerText(cell.value), canSelect: group.eligibleVariantKeys.length > 0 && (cell.state === 'value' || cell.state === 'null') } : group;
  }) : [];
  const reviewGroups = groups.map((group) => {
    const verdict = activeDraft ? matchAnswer(rule, expectedValue, group.value, { state: group.failed ? 'error' : group.canSelect ? 'value' : 'missing' }) : group.verdict;
    const isRejected = !expectedText && rejected.has(`${queueRow}:${field}:${group.key}`);
    const notes = group.variantStates.map((item) => ({ key: item.key, label: item.stale ? 'Source changed'
      : { match: 'Match', miss: 'Miss', unchecked: 'Not checked', unscored: 'To score', not_run: 'Not run' }[item.verdict] }));
    return { key: group.key, answerText: group.answerText, variantKeys: group.variantKeys,
      verdict: group.failed ? 'failed' as const : isRejected ? 'ruled_out' as const
        : verdict === 'unchecked' ? 'not_checked' as const : verdict === 'unscored' ? 'to_score' as const : verdict,
      canSelect: group.canSelect && (queue?.mode === 'disagreements' || rule === 'hand'),
      variantNotes: notes.some((item) => item.label === 'Source changed') || new Set(notes.map((item) => item.label)).size > 1
        ? notes.map((item) => `${item.key} · ${item.label}`).join(' · ') : undefined,
      ...(group.bounded ? { valueContent: loaded?.cells[group.key] ? <FocusedAnswerValue text={group.answerText} /> : <><p>{group.answerText}</p><small>Loading full answer…</small></> }
        : group.canSelect && queueRow !== undefined ? { valueContent: <ComparisonAnswerValue state={p.state} rowId={queueRow} field={field} value={group.value} /> } : {}),
    };
  });

  const decide = async (groupKey: string, accepted: boolean) => {
    const group = groups.find((item) => item.key === groupKey);
    if (!group || !group.canSelect || queueRow === undefined) return;
    if (queue?.mode === 'disagreements') {
      if (!accepted) { setRejected((previous) => new Set([...previous, `${queueRow}:${field}:${groupKey}`])); return; }
      const value = group.value == null || group.value === '' ? '[empty]' : group.value;
      if (expectedText && matchAnswer(rule, expectedValue, group.value) !== 'match') {
        const looser = MATCH_RULES.find((item) => item.value !== 'hand' && item.value !== rule
          && matchAnswer(item.value, expectedValue, group.value) === 'match')?.value ?? null;
        setConflict({ value, looser }); return;
      }
      if (!expectedText) await chooseExpected(value);
      else await flushExpected();
      return;
    }
    if (rule !== 'hand') return;
    if (!await flushExpected()) return;
    await e.mutate(async () => {
      let result = response;
      for (const key of group.eligibleVariantKeys) result = await p.api.setJudgment({ comparison: e.request, variant_key: key, row_id: queueRow, logical_key: field, verdict: accepted ? 'match' : 'miss' });
      if (result) e.setResponse(result);
    });
  };

  const moveQueue = async (delta: number) => {
    if (!await flushExpected()) return;
    const next = queueRows[queueRows.indexOf(queueRow!) + delta];
    if (next !== undefined && queue) { setDraft(null); setQueue({ ...queue, rowId: next }); }
  };
  const closeQueue = async () => { if (await flushExpected()) { setQueue(null); setDraft(null); } };

  const restore = async (id: number) => {
    setMenu(false);
    await e.mutate(async () => {
      const item = await p.api.getShelfItem(id);
      const settings = item.settings as unknown as Partial<GeneratedActionDraft>;
      const draft = settings.action_id && settings.scope && settings.params && settings.output_names
        ? settings as GeneratedActionDraft : p.state.context.draft;
      const savedDate = new Date(item.saved_at);
      const label = Number.isNaN(savedDate.getTime()) ? item.label : `${item.label} · ${savedDate.toLocaleDateString()}`;
      const restored = p.session.restoreVariant({ savedId: id, label, draft, result: { rows: Object.fromEntries(Object.entries(item.rows).map(([rowId, cells]) => [rowId,
        Object.fromEntries(Object.entries(cells).map(([key, { bounded_value, ...cell }]) => [key, { ...cell, evaluationValue: bounded_value }])),
      ])) } });
      if (!restored) throw new Error('A comparison can show up to 32 variants. Remove one before restoring another.');
    });
  };
  const shelve = async () => {
    if (!await flushExpected()) return;
    const keys = e.request.variants.filter((variant) => !p.state.variants.find((item) => item.displayKey === variant.key)?.readOnly).map((variant) => variant.key);
    const result = await e.mutate(() => p.api.saveShelf({ comparison: e.request, variant_keys: keys }));
    if (!result) return;
    await e.refreshShelf();
    if (p.onShelved) p.onShelved();
    else p.state.variants.filter((variant) => !variant.baseline).forEach((variant) => p.session.removeVariant(variant.id));
  };
  const bestKey = response?.best[field]?.[0];
  const bestVariant = p.state.variants.find((variant) => variant.displayKey === bestKey);
  const bestUse = bestVariant ? p.session.useVariant(bestVariant.id) : null;
  const evaluated = e.request.variants.length;
  const handBlocked = rule === 'hand' && unscored > 0;
  const scoreBusy = e.loading || e.saving || running;
  const selectField = async (next: string) => {
    if (!await flushExpected()) return;
    setField(next); setFilter('all'); setQueue(null);
  };

  return <>
    <div className="comparison-evaluation-toolbar">
      <div className="comparison-add-menu">
        <Button ref={menuAnchor} aria-expanded={menu} onClick={() => setMenu(!menu)}>+ ⌄</Button>
        {menu && <AnchoredMenu anchorRef={menuAnchor} onClose={() => setMenu(false)} width={380} className="comparison-menu" dismissOnFocusOutside>
          <button className="menu-item" role="menuitem" onClick={() => { const id = p.session.addVariant(); setMenu(false); if (id) p.onOpenVariant(id); }}>New variant</button>
          <button className="menu-item" role="menuitem" disabled={!modelField || !instructionsField || !p.renderVariantEditor} onClick={() => { setSeveralOpen(true); setMenu(false); }}>Try several…</button>
          <div className="comparison-menu-heading">From the shelf</div>
          {e.items.map((item) => <button key={item.id} className="menu-item" role="menuitem" onClick={() => void restore(item.id)}>{item.label} · {item.row_count} rows</button>)}
          {!e.items.length && <span className="comparison-menu-heading">No shelved results</span>}
          <button className="menu-item" role="menuitem" onClick={() => { setShelfOpen(true); setMenu(false); }}>Manage shelf…</button>
        </AnchoredMenu>}
      </div>
      <span>Compare to</span>
      <Button aria-pressed={lens === 'each_other'} disabled={p.state.variants.length < 2} title={p.state.variants.length < 2 ? 'Needs two variants' : undefined} onClick={() => { setLens('each_other'); setFilter('all'); }}>Each other</Button>
      <Button aria-pressed={lens === 'expected'} onClick={() => { setLens('expected'); e.refresh(); }}>✓ Expected answers</Button>
      {response && lens === 'each_other' && evaluated >= 2 && <Button onClick={() => setQueue({ mode: 'disagreements', rowId: disagreements[0] ?? 0, filter: 'all' })}>Variants disagree · {disagreements.length}</Button>}
      {response && lens === 'expected' && <><Button disabled={scoreBusy} onClick={() => setResultsOpen(true)}>Results</Button>
        {unscored > 0 && <Button disabled={scoreBusy} onClick={() => setQueue({ mode: 'score', rowId: response.row_ids[0] ?? 0, filter: 'to_score' })}>{unscored} to score by hand ›</Button>}
        <Button disabled={!e.link || sourceUnavailable || scoreBusy} onClick={() => setRuleOpen(true)}>{MATCH_RULES.find((item) => item.value === rule)?.label ?? 'Exact'} ▾</Button></>}
      <Button disabled={!evaluated || e.saving} onClick={() => void e.mutate(async () => download(await p.api.downloadCsv(e.request), 'comparison.csv'))}>⤓ CSV</Button>
      <Button disabled={!evaluated || e.saving} onClick={() => void e.mutate(async () => download(await p.api.downloadSettings(e.request), 'comparison-settings.json'))}>Settings</Button>
    </div>
    {e.error && <div className="action-compare-notice" role="alert">{e.error}<Button onClick={e.refresh}>Retry</Button></div>}
    <div className="comparison-field-toolbar">
      <div role="group" aria-label="Comparison field">{p.state.context.output_columns.map((column) => <Button key={column.logical_key} aria-pressed={field === column.logical_key} onClick={() => void selectField(column.logical_key)}>{column.current_name}</Button>)}</div>
      <span>{p.rowScopeTotal && p.rowScopeTotal > p.state.context.sample_row_ids.length
        ? `${p.state.context.sample_row_ids.length} of ${p.rowScopeTotal} ${p.rowScope === 'expected' ? 'with expected answers' : 'rows'}`
        : lens === 'expected' && e.link ? `${response?.variants[0]?.scores[field]?.checked ?? 0} with expected answers` : `${p.state.context.sample_row_ids.length} rows`}</span>
      {p.onChangeRowScope && <PanelSelect ariaLabel="Comparison rows" value={p.rowScope ?? 'sample'} disabled={running || p.rowScopePending}
        options={[{ value: 'sample', label: 'Sample · up to 20 rows' }, { value: 'all', label: 'All · up to 500 rows' }, { value: 'expected', label: 'All rows with expected answers · up to 500', disabled: !e.link || sourceUnavailable }]}
        onValueChange={(value) => { void flushExpected().then((saved) => { if (saved) p.onChangeRowScope?.(value as 'sample' | 'all' | 'expected', lens); }); }} />}
      {lens === 'expected' && e.link && <>{(['all', 'any_miss', 'best_misses', 'everyone_misses'] as const).map((value) => <Button key={value} aria-pressed={filter === value} disabled={scoreBusy || (value === 'best_misses' && !bestKey)} onClick={() => setFilter(value)}>
        {{ all: 'All', any_miss: 'Any miss', best_misses: 'Best misses', everyone_misses: 'Everyone misses' }[value]}
      </Button>)}</>}
      {e.loading && <span role="status">Scoring…</span>}
    </div>
    {lens === 'expected' && !e.link && <div className="action-compare-notice"><span>Join a sheet of expected answers to check this action.</span><Button onClick={p.onJoinExpected}>Join expected answers…</Button></div>}
    {lens === 'expected' && sourceUnavailable && e.link && <div>
      <UnavailableExpectedLinks links={[e.link]} onRemove={(id) => { void e.mutate(async () => { await p.api.deleteLink(id); p.onExpectedAnswersChanged?.(); }); }} />
      <Button onClick={p.onJoinExpected}>Edit join…</Button></div>}
    {queue && <ComparisonReview fieldLabel={currentField?.current_name ?? field} rowId={queueRow ?? 0}
      position={queueRow === undefined ? 0 : queueRows.indexOf(queueRow) + 1} total={queueRows.length}
      mode={queue.mode} groups={reviewGroups} expectedValue={expectedText} rule={rule} busy={scoreBusy} error={e.error}
      toolbar={<>
        <PanelSelect ariaLabel="Review field" value={field} disabled={scoreBusy}
          options={p.state.context.output_columns.map(column => ({ value: column.logical_key, label: column.current_name }))}
          onValueChange={(next) => { void flushExpected().then(saved => { if (saved) { setDraft(null); setField(next); } }); }} />
        {queue.mode !== 'disagreements' && <PanelSelect ariaLabel="Review answers" value={queue.filter} disabled={scoreBusy}
          options={[{ value: 'all', label: 'All answers' }, { value: 'to_score', label: 'To score by hand' }, { value: 'misses', label: 'Misses' }]}
          onValueChange={(next) => { void flushExpected().then(saved => { if (saved) setQueue({ ...queue, filter: next as 'all' | 'misses' | 'to_score' }); }); }} />}
      </>}
      emptyMessage={queueRow === undefined ? queue.mode === 'disagreements' ? 'No disagreements in this field.'
        : queue.filter === 'to_score' ? 'No answers left to score.' : 'No matching answers in this field.' : undefined}
      expectedContent={boundedExpected ? loaded?.expected ? <FocusedAnswerValue text={expectedText} /> : <p>Loading full expected answer…</p>
        : queueRow !== undefined && expectedValue != null && expectedValue !== '' ? <ComparisonAnswerValue state={p.state} rowId={queueRow} field={field} value={expectedValue} /> : undefined}
      expectedEditable={!boundedExpected}
      source={queueRow === undefined ? null : <ComparisonSources state={p.state} rowId={queueRow} field={field} />}
      onExpectedChange={expectedChanged} onRuleChange={(next) => void changeRule(next)} onAccept={(key) => void decide(key, true)} onReject={(key) => void decide(key, false)}
      onPrevious={() => void moveQueue(-1)} onNext={() => void moveQueue(1)} onClose={() => void closeQueue()} />}
    {p.renderGrid({ response, field, lens, rowIds: rows,
      onOpenRow: (rowId) => setQueue({ mode: rule === 'hand' ? 'score' : 'inspect', rowId, filter: 'all' }),
      onScoreClick: (key) => { if (!scoreBusy) setFilter(`variant:${key}`); }, onRuleClick: () => { if (!scoreBusy) setRuleOpen(true); } })}
    {!queue && p.state.variants.length === 1 && <div className="comparison-try-baseline"><strong>B · try to beat A</strong><Button onClick={() => { const id = p.session.addVariant(); if (id) p.onOpenVariant(id); }}>Another model ▾</Button><Button onClick={() => { const id = p.session.addVariant(); if (id) p.onOpenVariant(id); }}>Edit instructions</Button><Button disabled={!modelField || !instructionsField} onClick={() => setSeveralOpen(true)}>Try several…</Button></div>}
    {!queue && <footer className="comparison-footer"><Button disabled={!evaluated || running || e.saving} onClick={() => void shelve()}>🗄 Shelve</Button>
      {lens === 'each_other' && evaluated >= 2 && <Button onClick={() => setQueue({ mode: 'disagreements', rowId: disagreements[0] ?? 0, filter: 'all' })}>Check the {disagreements.length} disagreements</Button>}
      {response?.link && response.variants.some((variant) => variant.scores[field]?.checked > 0 && variant.scores[field].coverage < 1) && <span>Coverage differs · missing results are not checked</span>}
      {expectedField?.source === 'disagreements' && <span>From disagreements</span>}
      <span className="comparison-footer-spacer" />{bestUse && !handBlocked && <Button disabled={scoreBusy} className="btn-primary" onClick={() => p.onUseSettings(bestUse)}>Use {bestUse.variantLabel}…</Button>}</footer>}
    {ruleOpen && response && <MatchRuleDialog response={response} field={field} busy={e.saving || e.loading} onRule={(next) => void changeRule(next)} onEditAll={() => { setRuleOpen(false); p.onJoinExpected?.(); }} onClose={() => setRuleOpen(false)} />}
    {resultsOpen && response && <ComparisonResults response={response} onChoose={(nextField, variant) => { setField(nextField); setResultsOpen(false); setQueue({ mode: 'inspect', rowId: response.row_ids[0] ?? 0, filter: 'misses', variant }); }} onClose={() => setResultsOpen(false)} />}
    {shelfOpen && <ManageShelf items={e.items} busy={e.saving} onRename={(id, label) => void e.mutate(() => p.api.renameShelfItem(id, label))} onDelete={(id) => void e.mutate(() => p.api.deleteShelfItem(id))} onClose={() => setShelfOpen(false)} />}
    {severalOpen && modelField && instructionsField && <TrySeveral session={p.session} state={p.state} response={response} scoring={e.loading} modelField={modelField} instructionsField={instructionsField} renderVariantEditor={p.renderVariantEditor} onOpenVariant={(id) => { setSeveralOpen(false); p.onOpenVariant(id); }} onUse={p.onUseSettings} onClose={() => setSeveralOpen(false)} />}
    {conflict && queueRow !== undefined && <Modal title="Choose the expected answer" onClose={() => setConflict(null)} closeLabel="Cancel">
      <p className="comparison-dialog-copy">There is one expected answer per row.</p><div className="form-actions">
        {conflict.looser && <Button onClick={() => { void changeRule(conflict.looser!); setConflict(null); }}>{MATCH_RULES.find((item) => item.value === conflict.looser)?.label} would accept both</Button>}
        <Button onClick={() => { void chooseExpected(conflict.value); setConflict(null); }}>Replace expected answer</Button>
      </div></Modal>}
  </>;
}
