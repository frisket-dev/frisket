import type {
  ActionComparisonApi,
  ActionComparisonBoundedValue,
  ActionComparisonCell,
  ActionComparisonCellOrigin,
  ActionComparisonContext,
  ActionComparisonPreviewSnapshot,
} from '../api/actionComparison';
import { ApiError, ConfirmationRequiredError } from '../api/contractErrors';
import { pollPreviewJob } from '../api/previewJobTransport';
import { freshActionRequestKey, type GeneratedActionDraft, type JsonValue, type RegisteredActionRequest, type RunEstimate } from '../api/types';
import { jsonValuesEqual } from '../actions/canonicalJson';

export type ActionCompareVariantStatus =
  | 'pending' | 'running' | 'ready' | 'failed' | 'cancelled' | 'unavailable' | 'current';

export const ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE =
  'Preview no longer available; run this variant again.';

export interface ActionCompareResultCell {
  state: 'value' | 'null' | 'missing' | 'error' | 'pending' | 'cancelled';
  value?: JsonValue;
  bounded_value?: ActionComparisonBoundedValue | null;
  origin?: ActionComparisonCellOrigin;
  error?: string | null;
}

export interface ActionCompareVariantResult {
  rows: Record<string, Record<string, ActionCompareResultCell>>;
}

export interface ActionCompareVariant {
  id: string;
  displayKey: string;
  label: string;
  baseline: boolean;
  draft: GeneratedActionDraft;
  draftRevision: number;
  status: ActionCompareVariantStatus;
  previewId: string | null;
  result: ActionCompareVariantResult | null;
  previousResult: ActionCompareVariantResult | null;
  error: string | null;
  /** Every bounded preview chunk belongs to this variant until disposal. */
  previewIds?: string[];
  readOnly?: boolean;
  savedId?: number;
}

export interface ActionCompareCapturedSelection {
  rowIds: number[];
  totalCount: number;
}

export interface ActionCompareSessionInit {
  context: ActionComparisonContext;
  capturedSelection?: ActionCompareCapturedSelection | null;
}

export interface ActionCompareSessionSnapshot {
  revision: number;
  context: ActionComparisonContext;
  capturedSelection: ActionCompareCapturedSelection | null;
  variants: readonly ActionCompareVariant[];
  hiddenColumnKeys: readonly string[];
  highlightDifferences: boolean;
  sequenceStatus: 'idle' | 'running' | 'cancelled';
  activeVariantId: string | null;
  disposed: boolean;
}

export type ActionCompareRunOutcome =
  | { status: 'ready'; previewId: string; result: ActionCompareVariantResult }
  | { status: 'failed'; previewId?: string | null; error: string }
  | { status: 'cancelled'; previewId?: string | null }
  | { status: 'unavailable'; previewId?: string | null; error?: string };

export type ActionCompareVariantRun = (args: {
  context: ActionComparisonContext;
  variant: Readonly<ActionCompareVariant>;
  signal: AbortSignal;
  onStarted(previewId: string): void;
  onPartial?(result: ActionCompareVariantResult): void;
}) => Promise<ActionCompareRunOutcome>;

export interface ActionCompareSessionDependencies {
  runVariant: ActionCompareVariantRun;
  cancelPreview?(previewId: string): Promise<void>;
  discardPreview?(previewId: string): Promise<void>;
  createId?(): string;
  labelForDraft?(draft: GeneratedActionDraft, displayKey: string): string;
}

export type ActionCompareCostConfirmation = (
  estimate: RunEstimate,
  message: string,
  signal: AbortSignal,
) => Promise<boolean>;

export interface ActionCompareUseSettings {
  variantId: string;
  variantLabel: string;
  draft: GeneratedActionDraft;
  context: ActionComparisonContext;
  capturedSelection: ActionCompareCapturedSelection | null;
  sampleRowIds: number[];
}

export interface ActionCompareSession {
  getSnapshot(): ActionCompareSessionSnapshot;
  subscribe(listener: () => void): () => void;
  addVariant(fromVariantId?: string): string | null;
  restoreVariant(saved: { savedId: number; label: string; draft: GeneratedActionDraft; result: ActionCompareVariantResult }): string | null;
  updateVariant(variantId: string, draft: GeneratedActionDraft): boolean;
  removeVariant(variantId: string): boolean;
  retryVariant(variantId: string): boolean;
  markPreviewUnavailable(previewId: string): boolean;
  runPending(): Promise<void>;
  stop(): void;
  setColumnVisible(key: string, visible: boolean): void;
  setHighlightDifferences(value: boolean): void;
  useVariant(variantId: string): ActionCompareUseSettings | null;
  dispose(): Promise<void>;
}

let fallbackId = 0;
const defaultId = () => `action-compare-${++fallbackId}`;

function cloneJson<Value>(value: Value): Value {
  if (typeof structuredClone === 'function') return structuredClone(value);
  return JSON.parse(JSON.stringify(value)) as Value;
}

const cloneDraft = (draft: GeneratedActionDraft) => cloneJson(draft);

function resultFromBaseline(
  rows: Record<string, Record<string, ActionComparisonCell>> | null,
): ActionCompareVariantResult | null {
  return rows
    ? { rows: cloneJson(rows) as Record<string, Record<string, ActionCompareResultCell>> }
    : null;
}

function normalizePreviewResult(
  context: ActionComparisonContext,
  rows: ActionComparisonPreviewSnapshot['rows'],
): ActionCompareVariantResult {
  const logicalKeys = new Set<string>();
  const logicalKeyByCurrentName = new Map<string, string>();
  for (const output of context.output_columns) {
    if (logicalKeys.has(output.logical_key)) {
      throw new Error(`Comparison context repeats logical output ${JSON.stringify(output.logical_key)}.`);
    }
    logicalKeys.add(output.logical_key);
    const existing = logicalKeyByCurrentName.get(output.current_name);
    if (existing !== undefined && existing !== output.logical_key) {
      throw new Error(`Comparison context maps ${JSON.stringify(output.current_name)} to multiple outputs.`);
    }
    logicalKeyByCurrentName.set(output.current_name, output.logical_key);
  }

  return {
    rows: Object.fromEntries(Object.entries(rows).map(([rowId, cells]) => {
      const normalized: Record<string, ActionCompareResultCell> = {};
      for (const [currentName, cell] of Object.entries(cells)) {
        const logicalKey = logicalKeyByCurrentName.get(currentName);
        if (logicalKey === undefined) {
          throw new Error(`Comparison preview returned unexpected output ${JSON.stringify(currentName)}.`);
        }
        if (Object.prototype.hasOwnProperty.call(normalized, logicalKey)) {
          throw new Error(`Comparison preview returned output ${JSON.stringify(logicalKey)} more than once.`);
        }
        normalized[logicalKey] = cloneJson(cell) as ActionCompareResultCell;
      }
      return [rowId, normalized];
    })),
  };
}

function firstVariant(
  context: ActionComparisonContext,
  id: string,
  labelForDraft?: ActionCompareSessionDependencies['labelForDraft'],
): ActionCompareVariant {
  const current = context.baseline_mode === 'current_values';
  const available = context.available;
  const draft = cloneDraft(context.draft);
  return {
    id,
    displayKey: 'A',
    label: labelForDraft?.(draft, 'A') ?? (current ? 'Current values' : 'Original settings'),
    baseline: true,
    draft,
    draftRevision: 0,
    status: available ? (current ? 'current' : 'pending') : 'failed',
    previewId: null,
    result: available && current ? resultFromBaseline(context.baseline_rows) : null,
    previousResult: null,
    error: available ? null : context.reason?.message ?? 'This action cannot be compared.',
  };
}

function aborted(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}

export type ActionCompareCellEquality = 'same' | 'different' | 'not_comparable';

export function actionCompareCellEquality(
  left: ActionCompareResultCell | undefined,
  right: ActionCompareResultCell | undefined,
): ActionCompareCellEquality {
  if (!left || !right || left.bounded_value || right.bounded_value) return 'not_comparable';
  const leftSuccessful = left.state === 'value' || left.state === 'null';
  const rightSuccessful = right.state === 'value' || right.state === 'null';
  if (!leftSuccessful || !rightSuccessful) return 'not_comparable';
  if (left.state === 'null' || right.state === 'null') {
    return left.state === right.state ? 'same' : 'different';
  }
  if (left.value === undefined || right.value === undefined) return 'not_comparable';
  return jsonValuesEqual(left.value, right.value) ? 'same' : 'different';
}

export function createActionCompareSession(
  initial: ActionCompareSessionInit,
  dependencies: ActionCompareSessionDependencies,
): ActionCompareSession {
  const listeners = new Set<() => void>();
  const createId = dependencies.createId ?? defaultId;
  const previewIds = new Set<string>();
  const previewDiscards = new Map<string, Promise<void>>();
  let activeSequence: { stopRequested: boolean; promise: Promise<void> } | null = null;
  let activeRun: {
    variantId: string;
    draftRevision: number;
    controller: AbortController;
    previewId: string | null;
  } | null = null;
  let snapshot: ActionCompareSessionSnapshot;

  function makeInitialSnapshot(next: ActionCompareSessionInit): ActionCompareSessionSnapshot {
    return {
      revision: (snapshot?.revision ?? -1) + 1,
      context: next.context,
      capturedSelection: next.capturedSelection
        ? { rowIds: [...next.capturedSelection.rowIds], totalCount: next.capturedSelection.totalCount }
        : null,
      variants: [firstVariant(next.context, createId(), dependencies.labelForDraft)],
      hiddenColumnKeys: next.context.output_columns
        .filter((column) => column.hidden)
        .map((column) => `output:${column.logical_key}`),
      highlightDifferences: true,
      sequenceStatus: 'idle',
      activeVariantId: null,
      disposed: false,
    };
  }
  snapshot = makeInitialSnapshot(initial);

  function publish(patch: Partial<ActionCompareSessionSnapshot>): void {
    snapshot = { ...snapshot, ...patch, revision: snapshot.revision + 1 };
    for (const listener of listeners) listener();
  }

  function replaceVariant(
    variantId: string,
    update: (variant: ActionCompareVariant) => ActionCompareVariant,
  ): boolean {
    let changed = false;
    const variants = snapshot.variants.map((variant) => {
      if (variant.id !== variantId) return variant;
      changed = true;
      return update(variant);
    });
    if (changed) publish({ variants });
    return changed;
  }

  function rememberPreview(previewId: string): void {
    previewIds.add(previewId);
  }

  function discardRememberedPreview(previewId: string): Promise<void> {
    rememberPreview(previewId);
    const existing = previewDiscards.get(previewId);
    if (existing) return existing;
    const discard = Promise.resolve(dependencies.discardPreview?.(previewId))
      .then(() => { previewIds.delete(previewId); })
      .finally(() => { previewDiscards.delete(previewId); });
    previewDiscards.set(previewId, discard);
    return discard;
  }

  function cancelActive(variantId?: string): void {
    const active = activeRun;
    if (!active || (variantId && active.variantId !== variantId)) return;
    active.controller.abort();
    if (active.previewId) void dependencies.cancelPreview?.(active.previewId).catch(() => undefined);
  }

  function addVariant(fromVariantId?: string): string | null {
    if (snapshot.disposed || !snapshot.context.available) return null;
    const challengers = snapshot.variants.filter((variant) => !variant.baseline && !variant.readOnly).length;
    if (challengers >= snapshot.context.limits.max_challengers) return null;
    const used = new Set(snapshot.variants.map((variant) => variant.displayKey));
    const displayKey = Array.from({ length: snapshot.context.limits.max_challengers + 1 }, (_, index) => {
      let position = index + 1;
      let key = '';
      while (position > 0) { key = String.fromCharCode(65 + (position - 1) % 26) + key; position = Math.floor((position - 1) / 26); }
      return key;
    }).find((key) => key !== 'A' && !used.has(key));
    if (!displayKey) return null;
    const source = snapshot.variants.find((variant) => variant.id === fromVariantId) ?? snapshot.variants[0];
    const id = createId();
    const draft = cloneDraft(source.draft);
    publish({
      variants: [...snapshot.variants, {
        id,
        displayKey,
        label: dependencies.labelForDraft?.(draft, displayKey) ?? `Variant ${displayKey}`,
        baseline: false,
        draft,
        draftRevision: 0,
        status: 'pending',
        previewId: null,
        result: null,
        previousResult: null,
        error: null,
      }],
    });
    return id;
  }

  function restoreVariant(saved: { savedId: number; label: string; draft: GeneratedActionDraft; result: ActionCompareVariantResult }): string | null {
    if (snapshot.disposed || !snapshot.context.available) return null;
    const existing = snapshot.variants.find((variant) => variant.savedId === saved.savedId);
    if (existing) return existing.id;
    const count = snapshot.variants.filter((variant) => variant.readOnly).length;
    const id = createId();
    publish({ variants: [...snapshot.variants, {
      id, displayKey: count ? `S${count + 1}` : 'S', label: saved.label,
      baseline: false, draft: cloneDraft(saved.draft), draftRevision: 0,
      status: 'ready', previewId: null, previewIds: [], result: cloneJson(saved.result),
      previousResult: null, error: null, readOnly: true, savedId: saved.savedId,
    }] });
    return id;
  }

  function updateVariant(variantId: string, draft: GeneratedActionDraft): boolean {
    const current = snapshot.variants.find((variant) => variant.id === variantId);
    if (!current || current.baseline || current.readOnly || snapshot.disposed) return false;
    cancelActive(variantId);
    return replaceVariant(variantId, (variant) => ({
      ...variant,
      draft: cloneDraft(draft),
      label: dependencies.labelForDraft?.(draft, variant.displayKey) ?? variant.label,
      draftRevision: variant.draftRevision + 1,
      status: 'pending',
      previewId: null,
      previewIds: [],
      previousResult: variant.result ?? variant.previousResult,
      result: null,
      error: null,
    }));
  }

  function removeVariant(variantId: string): boolean {
    const variant = snapshot.variants.find((candidate) => candidate.id === variantId);
    if (!variant || variant.baseline || snapshot.disposed) return false;
    cancelActive(variantId);
    for (const previewId of variant.previewIds ?? (variant.previewId ? [variant.previewId] : [])) {
      void discardRememberedPreview(previewId).catch(() => undefined);
    }
    publish({ variants: snapshot.variants.filter((candidate) => candidate.id !== variantId) });
    return true;
  }

  function retryVariant(variantId: string): boolean {
    const current = snapshot.variants.find((variant) => variant.id === variantId);
    if (!current || current.readOnly || !['failed', 'cancelled', 'unavailable'].includes(current.status)) return false;
    return replaceVariant(variantId, (variant) => ({ ...variant, status: 'pending', previewId: null, previewIds: [], error: null }));
  }

  function markPreviewUnavailable(previewId: string): boolean {
    const current = snapshot.variants.find((variant) => variant.previewId === previewId || variant.previewIds?.includes(previewId));
    // A reader can outlive an edit or rerun. Only the exact completed preview
    // may retire its own result; a late 404 cannot clobber newer work.
    if (!current || current.status !== 'ready') return false;
    return replaceVariant(current.id, (variant) => ({
      ...variant,
      status: 'unavailable',
      result: null,
      previousResult: variant.result ?? variant.previousResult,
      error: ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE,
    }));
  }

  async function runOne(variantId: string, sequence: { stopRequested: boolean }): Promise<'continue' | 'stop'> {
    const variant = snapshot.variants.find((candidate) => candidate.id === variantId);
    if (!variant || variant.status !== 'pending' || sequence.stopRequested) return 'continue';
    const draftRevision = variant.draftRevision;
    const controller = new AbortController();
    activeRun = { variantId, draftRevision, controller, previewId: null };
    replaceVariant(variantId, (candidate) => ({ ...candidate, status: 'running', error: null }));
    publish({ activeVariantId: variantId });

    const onStarted = (previewId: string) => {
      rememberPreview(previewId);
      const current = snapshot.variants.find((candidate) => candidate.id === variantId);
      if (activeRun?.variantId !== variantId || activeRun.draftRevision !== draftRevision
        || current?.draftRevision !== draftRevision || current?.status !== 'running'
        || controller.signal.aborted || sequence.stopRequested) {
        void discardRememberedPreview(previewId).catch(() => undefined);
        return;
      }
      activeRun.previewId = previewId;
      replaceVariant(variantId, (candidate) => ({ ...candidate, previewId,
        previewIds: [...new Set([...(candidate.previewIds ?? []), previewId])],
      }));
    };

    let outcome: ActionCompareRunOutcome;
    try {
      outcome = await dependencies.runVariant({
        context: snapshot.context,
        variant: cloneJson(variant),
        signal: controller.signal,
        onStarted,
        onPartial: (result) => {
          const latest = snapshot.variants.find((candidate) => candidate.id === variantId);
          if (latest?.draftRevision === draftRevision && latest.status === 'running' && !controller.signal.aborted) {
            replaceVariant(variantId, (candidate) => ({ ...candidate, result: cloneJson(result) }));
          }
        },
      });
    } catch (error) {
      outcome = controller.signal.aborted || aborted(error)
        ? { status: 'cancelled', previewId: activeRun?.previewId }
        : { status: 'failed', previewId: activeRun?.previewId, error: error instanceof Error ? error.message : String(error) };
    }

    const outcomePreviewId = outcome.previewId ?? activeRun?.previewId ?? null;
    if (outcomePreviewId) rememberPreview(outcomePreviewId);
    const current = snapshot.variants.find((candidate) => candidate.id === variantId);
    const accepts = current?.draftRevision === draftRevision && current.status === 'running' && !sequence.stopRequested;
    if (accepts) {
      if (outcome.status === 'ready') {
        replaceVariant(variantId, (candidate) => ({
          ...candidate, status: 'ready', previewId: outcome.previewId,
          result: outcome.result, error: null,
        }));
      } else if (outcome.status === 'failed') {
        replaceVariant(variantId, (candidate) => ({
          ...candidate, status: 'failed', previewId: outcomePreviewId,
          result: null, error: outcome.error,
        }));
      } else if (outcome.status === 'unavailable') {
        replaceVariant(variantId, (candidate) => ({
          ...candidate, status: 'unavailable', previewId: outcomePreviewId,
          result: null,
          error: outcome.error ?? ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE,
        }));
      } else {
        replaceVariant(variantId, (candidate) => ({
          ...candidate, status: 'cancelled', previewId: outcomePreviewId,
          result: null, error: null,
        }));
      }
    }
    if (activeRun?.variantId === variantId && activeRun.draftRevision === draftRevision) {
      activeRun = null;
      publish({ activeVariantId: null });
    }
    return outcome.status === 'cancelled' ? 'stop' : 'continue';
  }

  function runPending(): Promise<void> {
    if (snapshot.disposed) return Promise.resolve();
    if (activeSequence) return activeSequence.promise;
    const sequence = { stopRequested: false, promise: Promise.resolve() };
    activeSequence = sequence;
    publish({ sequenceStatus: 'running' });
    const variantIds = snapshot.variants.filter((variant) => variant.status === 'pending').map((variant) => variant.id);
    sequence.promise = (async () => {
      let cancelled = false;
      for (const variantId of variantIds) {
        if (sequence.stopRequested) { cancelled = true; break; }
        if (await runOne(variantId, sequence) === 'stop') { cancelled = true; break; }
      }
      if (activeSequence === sequence) {
        activeSequence = null;
        publish({
          sequenceStatus: cancelled || sequence.stopRequested ? 'cancelled' : 'idle',
          activeVariantId: null,
        });
      }
    })();
    return sequence.promise;
  }

  function stop(): void {
    if (!activeSequence && !activeRun) return;
    if (activeSequence) activeSequence.stopRequested = true;
    const runningId = activeRun?.variantId;
    cancelActive();
    if (runningId) {
      replaceVariant(runningId, (variant) => variant.status === 'running'
        ? { ...variant, status: 'cancelled' }
        : variant);
    }
    publish({ sequenceStatus: 'cancelled', activeVariantId: null });
  }

  function setColumnVisible(key: string, visible: boolean): void {
    const hidden = new Set(snapshot.hiddenColumnKeys);
    if (visible) hidden.delete(key); else hidden.add(key);
    publish({ hiddenColumnKeys: [...hidden] });
  }

  function setHighlightDifferences(value: boolean): void {
    if (snapshot.highlightDifferences !== value) publish({ highlightDifferences: value });
  }

  function useVariant(variantId: string): ActionCompareUseSettings | null {
    const variant = snapshot.variants.find((candidate) => candidate.id === variantId);
    if (!variant || variant.readOnly || variant.status !== 'ready') return null;
    if (variant.baseline && snapshot.context.baseline_mode === 'current_values') return null;
    return {
      variantId: variant.id,
      variantLabel: variant.displayKey,
      draft: cloneDraft(variant.draft),
      context: snapshot.context,
      capturedSelection: snapshot.capturedSelection
        ? { ...snapshot.capturedSelection, rowIds: [...snapshot.capturedSelection.rowIds] }
        : null,
      sampleRowIds: [...snapshot.context.sample_row_ids],
    };
  }

  async function discardPreviews(): Promise<void> {
    stop();
    await activeSequence?.promise;
    await Promise.allSettled([...previewIds].map(discardRememberedPreview));
    previewIds.clear();
  }

  async function dispose(): Promise<void> {
    if (snapshot.disposed) return;
    await discardPreviews();
    publish({ disposed: true, variants: [], activeVariantId: null });
    listeners.clear();
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    addVariant,
    restoreVariant,
    updateVariant,
    removeVariant,
    retryVariant,
    markPreviewUnavailable,
    runPending,
    stop,
    setColumnVisible,
    setHighlightDifferences,
    useVariant,
    dispose,
  };
}

type GenericPreviewPoll = (
  api: Pick<ActionComparisonApi, 'getPreview' | 'cancelPreview'>,
  previewId: string,
  policy: {
    signal: AbortSignal;
    intervalMs: number;
    initialDelayMs: number;
    visibility: 'pause' | 'continue';
    maxFailures: number;
    cancelOnFailure: boolean;
  },
) => Promise<ActionComparisonPreviewSnapshot | null>;

// pollPreviewJob is value-shape agnostic: it only reads `status` and forwards
// the terminal payload. This typed view lets comparison retain JsonValue and
// bounded-artifact cells without widening the persisted grid CellValue type.
const pollComparisonPreview = pollPreviewJob as unknown as GenericPreviewPoll;

export function createActionCompareRunner(
  api: ActionComparisonApi,
  requestCostConfirmation: ActionCompareCostConfirmation,
): ActionCompareVariantRun {
  const runChunk: ActionCompareVariantRun = async ({ context, variant, signal, onStarted }) => {
    if (!context.available) {
      return { status: 'failed', error: context.reason?.message ?? 'This action cannot be compared.' };
    }
    if (variant.draft.scope.kind !== 'sheet_rows') {
      return { status: 'failed', error: 'Action comparison requires a sheet-row action.' };
    }
    let action: RegisteredActionRequest = {
      ...cloneDraft(variant.draft),
      scope: {
        ...variant.draft.scope,
        row_ids: [...context.sample_row_ids],
      },
      idempotency_key: freshActionRequestKey(variant.draft.action_id),
    };
    let started: Awaited<ReturnType<ActionComparisonApi['startPreview']>>;
    while (true) {
      if (signal.aborted) return { status: 'cancelled' };
      try {
        started = await api.startPreview({
          action,
          comparison: {
            basis: context.comparison_basis,
            expected_output_signature: context.expected_output_signature,
          },
        });
        break;
      } catch (error) {
        if (signal.aborted) return { status: 'cancelled' };
        if (!(error instanceof ConfirmationRequiredError)) {
          return { status: 'failed', error: error instanceof Error ? error.message : String(error) };
        }
        const approved = await requestCostConfirmation(error.estimate, error.message, signal);
        if (!approved || signal.aborted) return { status: 'cancelled' };
        if (!error.estimate.promise_set_hash) {
          return { status: 'failed', error: 'The server did not provide the confirmation hash for this preview.' };
        }
        // Preserve the exact draft, scope and retry identity that produced this
        // variant's 402. Only its own confirmation fact is added.
        action = { ...action, confirmation: error.estimate.promise_set_hash };
      }
    }

    onStarted(started.preview_id);
    if (signal.aborted) return { status: 'cancelled', previewId: started.preview_id };
    let preview: ActionComparisonPreviewSnapshot | null;
    try {
      preview = await pollComparisonPreview(api, started.preview_id, {
        signal,
        intervalMs: 250,
        initialDelayMs: 0,
        visibility: 'continue',
        maxFailures: 3,
        cancelOnFailure: true,
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        return {
          status: 'unavailable',
          previewId: started.preview_id,
          error: ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE,
        };
      }
      throw error;
    }
    if (!preview || preview.status === 'cancelled') {
      return { status: 'cancelled', previewId: started.preview_id };
    }
    if (preview.status === 'error') {
      return {
        status: 'failed',
        previewId: started.preview_id,
        error: preview.error?.message ?? 'The comparison preview failed.',
      };
    }
    if (preview.status !== 'done') {
      return { status: 'failed', previewId: started.preview_id, error: 'The comparison preview did not finish.' };
    }
    let result: ActionCompareVariantResult;
    try {
      result = normalizePreviewResult(context, preview.rows);
    } catch (error) {
      return {
        status: 'failed',
        previewId: started.preview_id,
        error: error instanceof Error ? error.message : 'The comparison preview outputs did not match its context.',
      };
    }
    return {
      status: 'ready',
      previewId: started.preview_id,
      result,
    };
  };

  return async ({ context, variant, signal, onStarted, onPartial }) => {
    // Ordinary previews retain their 20-row boundary. A comparison owns the
    // sequence of admitted chunks and their result/consent identities.
    const result: ActionCompareVariantResult = { rows: {} };
    let lastPreviewId = '';
    for (let offset = 0; offset < context.sample_row_ids.length; offset += 20) {
      if (signal.aborted) return { status: 'cancelled', previewId: lastPreviewId || null };
      const rows = context.sample_row_ids.slice(offset, offset + 20);
      const outcome = await runChunk({ context: { ...context, sample_row_ids: rows }, variant, signal, onStarted });
      if (outcome.status !== 'ready') return outcome;
      lastPreviewId = outcome.previewId;
      Object.assign(result.rows, outcome.result.rows);
      onPartial?.(result);
    }
    return { status: 'ready', previewId: lastPreviewId, result };
  };
}
