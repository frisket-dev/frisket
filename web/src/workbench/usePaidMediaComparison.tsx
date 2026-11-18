import { useCallback, useRef, useState } from 'react';

import type { ProjectApi } from '../api/ports';
import type { PreviewSampleResult, PreviewStartResult, RunEstimate } from '../api/types';
import { pollPreviewJob } from '../api/previewJobTransport';
import { estimatedUsd } from '../actions/estimatedCost';
import { CostGateModal } from '../components/CostGateModal';
import { formatUsd } from '../format';
import type { CompareColumn, MediaCompareConfig, ScratchDoc } from './mediaCompareSession';
import { errorMessage } from '../hooks/errorMessage';

interface ComparisonCalls<R, Input> {
  api: Pick<ProjectApi, 'getPreview' | 'cancelPreview'>;
  inputFor(doc: ScratchDoc<R>, column: CompareColumn): Input;
  estimate(file: File, input: Input): Promise<RunEstimate>;
  start(file: File, input: Input & { confirmation?: string }): Promise<PreviewStartResult>;
  readResult(preview: PreviewSampleResult, engineId: string): R;
}

type Estimate<Input> = { input: Input; estimate: RunEstimate } | { error: string };
const pairKey = (doc: { id: string }, column: CompareColumn) => `${doc.id}:${column.id}`;

/** Shared by the two upload comparisons: quote first, confirm once, then
 * retain each candidate's exact input and token through normal preview jobs. */
export function usePaidMediaComparison<R, Input>(calls: ComparisonCalls<R, Input>) {
  const estimates = useRef(new Map<string, Estimate<Input>>());
  const [currentEstimate, setCurrentEstimate] = useState<RunEstimate | null>(null);
  const [consent, setConsent] = useState<{
    estimate: RunEstimate; message: string; finish(accepted: boolean): void;
  } | null>(null);
  const clearEstimate = useCallback(() => setCurrentEstimate(null), []);

  const prepareRun: NonNullable<MediaCompareConfig<R>['prepareRun']> = async (pairs, signal) => {
    // A changed selection only earns a new display after this batch's exact
    // inputs have been quoted. Never leave an old amount beside a new run.
    clearEstimate();
    const prepared = new Map<string, Estimate<Input>>();
    const paid: Array<{ estimate: RunEstimate; label: string }> = [];
    const estimated: Array<{ estimate: RunEstimate; label: string }> = [];
    for (const { doc, column } of pairs) {
      if (signal.aborted) return false;
      try {
        // Inputs have already been projected from the selected catalog engine
        // by the tab; never recover consent from an earlier run.
        const input = calls.inputFor(doc, column);
        const estimate = await calls.estimate(doc.file, input);
        if (estimate.requires_confirmation && !estimate.promise_set_hash) {
          throw new Error('The estimate is missing its required confirmation token.');
        }
        prepared.set(pairKey(doc, column), { input, estimate });
        estimated.push({ estimate, label: `${doc.filename}: ${column.engineId}` });
        if (estimate.requires_confirmation) paid.push({ estimate, label: `${doc.filename}: ${column.engineId}` });
      } catch (error) {
        prepared.set(pairKey(doc, column), { error: errorMessage(error) });
      }
    }
    if (signal.aborted) return false;
    estimates.current = prepared;
    const estimatePrices = estimated.map(({ estimate }) => estimatedUsd(estimate));
    const estimateKnown = estimatePrices.every((price): price is number => price !== null);
    setCurrentEstimate({
      cost: null,
      rows: estimated.length,
      policy_id: 'media_compare',
      billed_cost: estimateKnown
        ? Math.round(estimatePrices.reduce((sum, price) => sum + price, 0) * 1_000_000)
        : null,
    });
    if (!paid.length) return true;
    const prices = paid.map(({ estimate }) => estimatedUsd(estimate));
    const known = prices.every((price): price is number => price !== null);
    const estimate: RunEstimate = {
      cost: null, rows: paid.length, policy_id: 'media_compare',
      billed_cost: known ? Math.round(prices.reduce((sum, price) => sum + price, 0) * 1_000_000) : null,
      claims: paid.flatMap(({ estimate, label }, index) => (estimate.claims ?? []).map((claim) => ({
        field: `${index}:${claim.field}`, display: `${label} — ${claim.display}`,
      }))),
    };
    return new Promise<boolean>((resolve) => {
      const finish = (accepted: boolean) => {
        signal.removeEventListener('abort', cancel);
        setConsent(null);
        resolve(accepted && !signal.aborted);
      };
      const cancel = () => finish(false);
      signal.addEventListener('abort', cancel, { once: true });
      setConsent({ estimate, message: `Run ${paid.length} paid or external preview${paid.length === 1 ? '' : 's'}: ${paid.map(({ label }) => label).join(', ')}. Results are temporary; usage is recorded.`, finish });
    });
  };

  const runColumn: MediaCompareConfig<R>['runColumn'] = async (doc, column, _engine, signal) => {
    const candidate = estimates.current.get(pairKey(doc, column));
    if (!candidate) return { ok: false, message: 'Estimate this comparison before running it.' };
    if ('error' in candidate) return { ok: false, message: candidate.error };
    if (signal.aborted) return { ok: false, message: 'Comparison cancelled.' };
    const startedAt = performance.now();
    const started = await calls.start(doc.file, {
      ...candidate.input,
      ...(candidate.estimate.requires_confirmation ? { confirmation: candidate.estimate.promise_set_hash } : {}),
    });
    const preview = await pollPreviewJob(calls.api, started.previewId, {
      signal, intervalMs: 250, initialDelayMs: 0, visibility: 'continue',
      maxFailures: 1, cancelOnFailure: true,
    });
    if (!preview) return { ok: false, message: 'Comparison cancelled.' };
    const rows = preview.kind === 'table' ? preview.rows : Object.values(preview.rows);
    const error = preview.error?.message ?? rows.flatMap((row) => Object.values(row)).find((cell) => cell.error)?.error;
    if (error || preview.status !== 'done') return { ok: false, message: error ?? 'Comparison cancelled.' };
    return { ok: true, results: calls.readResult(preview, column.engineId!), confidence: null,
      preview, elapsedMs: Math.max(0, performance.now() - startedAt) };
  };

  const currentEstimateUsd = estimatedUsd(currentEstimate);
  const estimateLine = currentEstimate && (
    <div className="cost-line cost-paid" data-testid="media-compare-cost-estimate">
      <span className="cost-dot" aria-hidden />
      Comparison estimate: <strong>{currentEstimateUsd === null
        ? 'UNKNOWN' : formatUsd(currentEstimateUsd)}</strong>
      {currentEstimate.rows > 0 && <> · {currentEstimate.rows} variant{currentEstimate.rows === 1 ? '' : 's'}</>}
    </div>
  );

  return { prepareRun, runColumn, estimateLine, clearEstimate, gate: consent && <CostGateModal estimate={consent.estimate}
    message={consent.message} onConfirm={() => consent.finish(true)} onCancel={() => consent.finish(false)} /> };
}
