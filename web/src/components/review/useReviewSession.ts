import { useCallback, useEffect, useRef, useState } from 'react';
import type { CellValue, ReviewAction, ReviewBundleField, ReviewBundleOptions, ReviewBundlePage } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { applyDecision, isDecided, nextUndecidedField } from './reviewDecisions';

export interface ReviewSessionOptions {
  runId: string;
  options: ReviewBundleOptions;
  readOnly?: boolean;
  onDecisionSaved?(): void;
  onChanged(remaining: number): void;
}

/** Corrections are text edits. Structured and typed values keep their shape. */
export function isPlainTextReviewField(field: ReviewBundleField | null | undefined): boolean {
  if (!field || field.columnType !== 'text' || (field.value !== null && typeof field.value !== 'string')) return false;
  const format = field.format;
  if (format && format !== 'plain_text') return false;
  if (format === 'plain_text' || typeof field.value !== 'string') return true;
  const trimmed = field.value.trim();
  if (!trimmed.startsWith('[') && !trimmed.startsWith('{')) return true;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed === null || typeof parsed !== 'object';
  } catch {
    return true;
  }
}

/** Keeps a stable page of row bundles. Decisions never remove the current row. */
export function useReviewSession({ runId, options, readOnly = false, onDecisionSaved, onChanged }: ReviewSessionOptions) {
  const { projectApi: api, chromePreferences } = useWorkspaceStores();
  const [page, setPage] = useState<ReviewBundlePage | null>(null);
  const [cursor, setCursor] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [note, setNote] = useState('');
  const noteDraft = useRef({ rowId: '', value: '', saved: '' });
  const noteRequest = useRef<Promise<boolean> | null>(null);
  const [busy, setBusy] = useState(false);
  const [savingNote, setSavingNote] = useState(false);
  const mutation = useRef(false);
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState<string | null>(null);
  const request = useRef(0);
  const bundle = page?.bundles[cursor];
  const field = bundle?.fields.find((item) => item.id === selectedId) ?? bundle?.fields[0];

  const selectRow = useCallback((loaded: ReviewBundlePage, index: number) => {
    const row = loaded.bundles[index];
    setCursor(index);
    setSelectedId(row?.fields.find((item) => !isDecided(item))?.id ?? row?.fields[0]?.id ?? null);
    setEditingId(null);
    const value = row?.reviewNote ?? '';
    noteDraft.current = { rowId: row?.rowId ?? '', value, saved: value };
    setNote(value);
  }, []);

  const loadPage = useCallback((offset: number, last = false) => {
    const generation = ++request.current;
    return api.getReviewBundles(offset, 25, runId, true, options).then((response) => {
      if (generation !== request.current) return;
      const loaded = options.fieldId ? { ...response, bundles: response.bundles.map((row) => ({
        ...row, fields: row.fields.filter((item) => item.columnId === options.fieldId),
      })) } : response;
      setPage(loaded);
      selectRow(loaded, last ? Math.max(0, loaded.bundles.length - 1) : 0);
      setProblem(null);
    }).catch(() => {
      if (generation === request.current) setProblem('Could not load review results. Please try again.');
    }).finally(() => {
      if (generation === request.current) setLoading(false);
    });
  }, [api, options, runId, selectRow]);

  useEffect(() => {
    void loadPage(0);
    return () => { request.current += 1; };
  }, [loadPage]);

  const changeNote = (value: string) => {
    noteDraft.current.value = value;
    setNote(value);
  };
  const saveNote = useCallback((): Promise<boolean> => {
    if (noteRequest.current) return noteRequest.current;
    const draft = noteDraft.current;
    if (readOnly || !draft.rowId || draft.value === draft.saved) return Promise.resolve(true);
    const value = draft.value;
    setSavingNote(true);
    const pending = api.setReviewNote(runId, draft.rowId, value.trim() || null).then(() => {
      draft.saved = value;
      setPage((current) => current && ({ ...current, bundles: current.bundles.map((row) => (
        row.rowId === draft.rowId ? { ...row, reviewNote: value.trim() || null } : row
      )) }));
      setProblem(null);
      return true;
    }).catch(() => {
      setProblem('Could not save your row note. Try again before leaving this row.');
      return false;
    }).finally(() => {
      noteRequest.current = null;
      setSavingNote(false);
    });
    noteRequest.current = pending;
    return pending;
  }, [api, readOnly, runId]);

  const leave = useCallback((action: () => void) => {
    if (mutation.current) return;
    void saveNote().then((saved) => { if (saved) action(); });
  }, [saveNote]);

  const moveRow = (delta: -1 | 1) => leave(() => {
    if (!page) return;
    const next = cursor + delta;
    if (next >= 0 && next < page.bundles.length) selectRow(page, next);
    else if (delta === 1 && page.nextOffset !== null) { setLoading(true); void loadPage(page.nextOffset); }
    else if (delta === -1 && page.offset > 0) { setLoading(true); void loadPage(Math.max(0, page.offset - page.limit), true); }
  });

  const selectField = (id: string) => { setSelectedId(id); setEditingId(null); };
  const moveField = (delta: -1 | 1) => {
    if (!bundle?.fields.length || !field) return;
    const index = bundle.fields.findIndex((item) => item.id === field.id);
    selectField(bundle.fields[(index + delta + bundle.fields.length) % bundle.fields.length].id);
  };
  const startEdit = (target = field) => {
    if (!target || !isPlainTextReviewField(target) || busy || readOnly) return;
    setSelectedId(target.id);
    setEditingId(target.id);
    setEditValue(target.value === null ? '' : String(target.value));
  };

  const resolve = async (targets: ReviewBundleField[], action: ReviewAction, value?: CellValue) => {
    if (readOnly || mutation.current || !bundle || !targets.length || loading) return false;
    mutation.current = true;
    if (!await saveNote()) { mutation.current = false; return false; }
    setBusy(true);
    setProblem(null);
    let updated = { ...bundle, reviewNote: noteDraft.current.saved.trim() || null };
    let saved = 0;
    try {
      for (const target of targets) {
        await api.reviewItem(target.id, action, value);
        updated = { ...updated, fields: updated.fields.map((item) => item.id === target.id ? applyDecision(item, action, value) : item) };
        const next = updated;
        setPage((current) => current && ({ ...current, bundles: current.bundles.map((row) => row.id === next.id ? next : row) }));
        saved += 1;
      }
      setSelectedId(action === 'clear' ? targets[0].id : nextUndecidedField(updated, targets[targets.length - 1].id));
      setEditingId(null);
      return true;
    } catch {
      if (saved) setSelectedId(nextUndecidedField(updated, targets[saved - 1].id));
      setProblem(saved ? `${saved} decisions saved; the remaining decisions could not be saved. Please try again.` : 'Could not save your review decision. Please try again.');
      return false;
    } finally {
      mutation.current = false;
      setBusy(false);
      if (saved) {
        onDecisionSaved?.();
        void api.getReviewCount().then(onChanged).catch(() => setProblem('Your decisions were saved, but the pending count could not be refreshed.'));
      }
    }
  };

  const toggle = (target: ReviewBundleField, verdict: 'accept' | 'reject') => {
    const active = verdict === 'accept' ? target.reviewDecision === 'accept' : target.reviewState === 'rejected';
    void resolve([target], active ? 'clear' : verdict);
  };
  const acceptRemaining = () => {
    if (bundle) void resolve(bundle.fields.filter((item) => !isDecided(item)), 'accept')
      .then((saved) => { if (saved) moveRow(1); });
  };
  const reset = () => { if (bundle) void resolve(bundle.fields.filter(isDecided), 'clear'); };

  return {
    bundle, field, page, cursor, note, changeNote, saveNote, savingNote, busy: busy || loading, loading,
    problem, retry: () => { setLoading(true); void loadPage(page?.offset ?? 0); }, readOnly, leave,
    moveRow, moveField, selectField, startEdit, editingId, editValue, setEditValue,
    cancelEdit: () => setEditingId(null), resolve, toggle, acceptRemaining, reset,
    projectId: chromePreferences.projectId,
    canPrevious: !!page && page.offset + cursor > 0,
    canNext: !!page && (cursor < page.bundles.length - 1 || page.hasMore),
  };
}
export type ReviewSessionController = ReturnType<typeof useReviewSession>;
