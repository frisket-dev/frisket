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

interface RowPageTarget {
  cursor?: number;
  index: number;
  start: number;
}

interface RandomPageTarget {
  rowIds: string[];
  index: number;
  start: number;
  frontierHasMore: boolean;
}

type RandomPageRequest =
  | { kind: 'sample'; excludeRowIds: string[]; index: number; start: number }
  | { kind: 'rows'; target: RandomPageTarget };

interface PageLoadRequest {
  offset: number;
  last: boolean;
  rowTarget?: RowPageTarget;
  randomRequest?: RandomPageRequest;
}

const FIRST_ROW_PAGE: RowPageTarget = { index: 0, start: 0 };

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
  const failedPageLoad = useRef<PageLoadRequest | null>(null);
  const [canRetryPageLoad, setCanRetryPageLoad] = useState(false);
  const rowPages = useRef<RowPageTarget[]>([FIRST_ROW_PAGE]);
  const currentRowPage = useRef<RowPageTarget>(FIRST_ROW_PAGE);
  const [rowPage, setRowPage] = useState<RowPageTarget>(FIRST_ROW_PAGE);
  const randomPages = useRef<RandomPageTarget[]>([]);
  const currentRandomPage = useRef<RandomPageTarget | null>(null);
  const [randomPage, setRandomPage] = useState<RandomPageTarget | null>(null);
  const [randomPageCount, setRandomPageCount] = useState(0);
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

  const loadPage = useCallback((
    offset: number,
    last = false,
    rowTarget?: RowPageTarget,
    randomRequest?: RandomPageRequest,
  ) => {
    const attemptedLoad = { offset, last, rowTarget, randomRequest };
    const generation = ++request.current;
    const rowOrdered = options.order === 'row';
    const randomOrdered = options.order === 'shuffle';
    const requestOptions: ReviewBundleOptions = rowOrdered
      ? { fieldId: options.fieldId, order: 'row', cursor: rowTarget?.cursor }
      : randomOrdered
        ? {
            fieldId: options.fieldId,
            order: 'shuffle',
            ...(randomRequest?.kind === 'rows'
              ? { rowIds: randomRequest.target.rowIds }
              : { excludeRowIds: randomRequest?.excludeRowIds ?? [] }),
          }
        : options;
    return api.getReviewBundles(rowOrdered || randomOrdered ? 0 : offset, 25, runId, true, requestOptions).then((response) => {
      if (generation !== request.current) return;
      const loaded = options.fieldId ? { ...response, bundles: response.bundles.map((row) => ({
        ...row, fields: row.fields.filter((item) => item.columnId === options.fieldId),
      })) } : response;
      if (rowOrdered && rowTarget) {
        currentRowPage.current = rowTarget;
        setRowPage(rowTarget);
      }
      if (randomOrdered && randomRequest) {
        const target = randomRequest.kind === 'rows'
          ? randomRequest.target
          : {
              rowIds: loaded.bundles.map((item) => item.rowId),
              index: randomRequest.index,
              start: randomRequest.start,
              frontierHasMore: response.hasMore,
            };
        if (randomRequest.kind === 'sample') {
          randomPages.current = [...randomPages.current.slice(0, target.index), target];
          setRandomPageCount(randomPages.current.length);
        }
        currentRandomPage.current = target;
        setRandomPage(target);
      }
      setPage(loaded);
      selectRow(loaded, last ? Math.max(0, loaded.bundles.length - 1) : 0);
      failedPageLoad.current = null;
      setCanRetryPageLoad(false);
      setProblem(null);
    }).catch(() => {
      if (generation === request.current) {
        failedPageLoad.current = attemptedLoad;
        setCanRetryPageLoad(true);
        setProblem('Could not load review results. Please try again.');
      }
    }).finally(() => {
      if (generation === request.current) setLoading(false);
    });
  }, [api, options, runId, selectRow]);

  useEffect(() => {
    rowPages.current = [FIRST_ROW_PAGE];
    currentRowPage.current = FIRST_ROW_PAGE;
    randomPages.current = [];
    currentRandomPage.current = null;
    failedPageLoad.current = null;
    void loadPage(0, false, FIRST_ROW_PAGE, options.order === 'shuffle'
      ? { kind: 'sample', excludeRowIds: [], index: 0, start: 0 }
      : undefined);
    return () => { request.current += 1; };
  }, [loadPage, options.order]);

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
    else if (delta === 1 && options.order === 'shuffle' && randomPage) {
      const stored = randomPages.current[randomPage.index + 1];
      setLoading(true);
      if (stored) void loadPage(0, false, undefined, { kind: 'rows', target: stored });
      else if (randomPage.frontierHasMore) {
        const excludeRowIds = [...new Set(randomPages.current.flatMap((target) => target.rowIds))];
        void loadPage(0, false, undefined, {
          kind: 'sample',
          excludeRowIds,
          index: randomPage.index + 1,
          start: randomPage.start + page.bundles.length,
        });
      } else setLoading(false);
    } else if (delta === -1 && options.order === 'shuffle' && randomPage?.index) {
      const target = randomPages.current[randomPage.index - 1];
      setLoading(true);
      void loadPage(0, true, undefined, { kind: 'rows', target });
    } else if (delta === 1 && options.order === 'row' && page.nextCursor !== null) {
      const target = {
        cursor: page.nextCursor,
        index: rowPage.index + 1,
        start: rowPage.start + page.bundles.length,
      };
      rowPages.current = [...rowPages.current.slice(0, target.index), target];
      setLoading(true);
      void loadPage(0, false, target);
    } else if (delta === 1 && page.nextOffset !== null) {
      setLoading(true);
      void loadPage(page.nextOffset);
    } else if (delta === -1 && options.order === 'row' && rowPage.index > 0) {
      const target = rowPages.current[rowPage.index - 1];
      setLoading(true);
      void loadPage(0, true, target);
    } else if (delta === -1 && page.offset > 0) {
      setLoading(true);
      void loadPage(Math.max(0, page.offset - page.limit), true);
    }
  });

  const selectField = (id: string) => { setSelectedId(id); setEditingId(null); };
  const moveField = (delta: -1 | 1) => {
    if (!bundle?.fields.length || !field) return;
    const index = bundle.fields.findIndex((item) => item.id === field.id);
    selectField(bundle.fields[(index + delta + bundle.fields.length) % bundle.fields.length].id);
  };
  const startEdit = (target = field) => {
    if (!target || target.canEdit === false || !isPlainTextReviewField(target) || busy || readOnly) return;
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
    problem, canRetryPageLoad, retry: () => {
      setLoading(true);
      const failed = failedPageLoad.current;
      if (failed) {
        void loadPage(failed.offset, failed.last, failed.rowTarget, failed.randomRequest);
        return;
      }
      const target = currentRandomPage.current;
      void loadPage(
        page?.offset ?? 0,
        false,
        options.order === 'row' ? currentRowPage.current : undefined,
        options.order === 'shuffle'
          ? target
            ? { kind: 'rows', target }
            : { kind: 'sample', excludeRowIds: [], index: 0, start: 0 }
          : undefined,
      );
    }, readOnly, leave,
    moveRow, moveField, selectField, startEdit, editingId, editValue, setEditValue,
    cancelEdit: () => setEditingId(null), resolve, toggle, acceptRemaining, reset,
    projectId: chromePreferences.projectId,
    position: page ? (options.order === 'row' ? rowPage.start
      : options.order === 'shuffle' ? randomPage?.start ?? 0 : page.offset) + cursor + 1 : 0,
    canPrevious: !!page && (cursor > 0 || (options.order === 'row' ? rowPage.index > 0
      : options.order === 'shuffle' ? !!randomPage?.index : page.offset > 0)),
    canNext: !!page && (cursor < page.bundles.length - 1
      || (options.order === 'row' ? page.nextCursor !== null
        : options.order === 'shuffle'
          ? !!randomPage && (randomPage.index < randomPageCount - 1 || randomPage.frontierHasMore)
          : page.hasMore)),
  };
}
export type ReviewSessionController = ReturnType<typeof useReviewSession>;
