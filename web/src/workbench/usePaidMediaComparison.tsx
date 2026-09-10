import { useCallback, useRef, useState } from 'react';

import type { ProjectApiPort } from '../api/ports';
import type { PreviewSampleResult, PreviewStartResult, RunEstimate } from '../api/types';
import { quotedUsd } from '../actions/quotedCost';
import { CostGateModal } from '../components/CostGateModal';
import { formatUsd } from '../format';
import type { CompareColumn, MediaCompareConfig, ScratchDoc } from './mediaCompareSession';

interface ComparisonCalls<R, Input> {
  api: Pick<ProjectApiPort, 'getPreview' | 'cancelPreview'>;
  inputFor(doc: ScratchDoc<R>, column: CompareColumn): Input;
  estimate(file: File, input: Input): Promise<RunEstimate>;
  start(file: File, input: Input & { confirmation?: string }): Promise<PreviewStartResult>;
  readResult(preview: PreviewSampleResult, engineId: string): R;
}

type Quote<Input> = { input: Input; estimate: RunEstimate } | { error: string };
const pairKey = (doc: { id: string }, column: CompareColumn) => `${doc.id}:${column.id}`;

/** Shared by the two upload comparisons: quote first, confirm once, then
 * retain each candidate's exact input and token through normal preview jobs. */
export function usePaidMediaComparison<R, Input>(calls: ComparisonCalls<R, Input>) {
  const quotes = useRef(new Map<string, Quote<Input>>());
  const [currentQuote, setCurrentQuote] = useState<RunEstimate | null>(null);
  const [consent, setConsent] = useState<{
    estimate: RunEstimate; message: string; finish(accepted: boolean): void;
  } | null>(null);
  const clearQuote = useCallback(() => setCurrentQuote(null), []);

  const prepareRun: NonNullable<MediaCompareConfig<R>['prepareRun']> = async (pairs, signal) => {
    // A changed selection only earns a new display after this batch's exact
    // inputs have been quoted. Never leave an old amount beside a new run.
    clearQuote();
    const prepared = new Map<string, Quote<Input>>();
    const paid: Array<{ estimate: RunEstimate; label: string }> = [];
    const quoted: Array<{ estimate: RunEstimate; label: string }> = [];
    for (const { doc, column } of pairs) {
      if (signal.aborted) return false;
      try {
        // Inputs have already been projected from the selected catalog engine
        // by the tab; never recover consent from an earlier run.
        const input = calls.inputFor(doc, column);
        const estimate = await calls.estimate(doc.file, input);
        if (estimate.requires_confirmation && !estimate.promise_set_hash) {
          throw new Error('The quote is missing its required confirmation token.');
        }
        prepared.set(pairKey(doc, column), { input, estimate });
        quoted.push({ estimate, label: `${doc.filename}: ${column.engineId}` });
        if (estimate.requires_confirmation) paid.push({ estimate, label: `${doc.filename}: ${column.engineId}` });
      } catch (error) {
        prepared.set(pairKey(doc, column), { error: error instanceof Error ? error.message : String(error) });
      }
    }
    if (signal.aborted) return false;
    quotes.current = prepared;
    const quotePrices = quoted.map(({ estimate }) => quotedUsd(estimate));
    const quoteKnown = quotePrices.every((price): price is number => price !== null);
    setCurrentQuote({
      cost: null,
      rows: quoted.length,
      policy_id: 'media_compare',
      billed_cost: quoteKnown
        ? Math.round(quotePrices.reduce((sum, price) => sum + price, 0) * 1_000_000)
        : null,
    });
    if (!paid.length) return true;
    const prices = paid.map(({ estimate }) => quotedUsd(estimate));
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
      setConsent({ estimate, message: `Approve ${paid.length} paid or external preview${paid.length === 1 ? '' : 's'}: ${paid.map(({ label }) => label).join(', ')}. Results are temporary; usage is recorded.`, finish });
    });
  };

  const runColumn: MediaCompareConfig<R>['runColumn'] = async (doc, column, _engine, signal) => {
    const quote = quotes.current.get(pairKey(doc, column));
    if (!quote) return { ok: false, message: 'Estimate this comparison before running it.' };
    if ('error' in quote) return { ok: false, message: quote.error };
    if (signal.aborted) return { ok: false, message: 'Comparison cancelled.' };
    let previewId: string | null = null;
    let terminal = false;
    const startedAt = performance.now();
    try {
      const started = await calls.start(doc.file, {
        ...quote.input,
        ...(quote.estimate.requires_confirmation ? { confirmation: quote.estimate.promise_set_hash } : {}),
      });
      previewId = started.previewId;
      while (!signal.aborted) {
        const preview = await calls.api.getPreview(previewId);
        if (preview.status !== 'running') {
          terminal = true;
          const rows = preview.kind === 'table' ? preview.rows : Object.values(preview.rows);
          const error = preview.error?.message ?? rows.flatMap((row) => Object.values(row)).find((cell) => cell.error)?.error;
          if (error || preview.status !== 'done') return { ok: false, message: error ?? 'Comparison cancelled.' };
          return { ok: true, results: calls.readResult(preview, column.engineId!), confidence: null,
            preview, elapsedMs: Math.max(0, performance.now() - startedAt) };
        }
        await new Promise<void>((resolve) => window.setTimeout(resolve, 250));
      }
      return { ok: false, message: 'Comparison cancelled.' };
    } finally {
      if (previewId && !terminal) await calls.api.cancelPreview(previewId).catch(() => undefined);
    }
  };

  const currentQuoteUsd = quotedUsd(currentQuote);
  const quote = currentQuote && (
    <div className="cost-line cost-paid" data-testid="media-compare-cost-estimate">
      <span className="cost-dot" aria-hidden />
      Comparison estimate: <strong>{currentQuoteUsd === null
        ? 'UNKNOWN' : formatUsd(currentQuoteUsd)}</strong>
      {currentQuote.rows > 0 && <> · {currentQuote.rows} variant{currentQuote.rows === 1 ? '' : 's'}</>}
    </div>
  );

  return { prepareRun, runColumn, quote, clearQuote, gate: consent && <CostGateModal estimate={consent.estimate}
    message={consent.message} onConfirm={() => consent.finish(true)} onCancel={() => consent.finish(false)} /> };
}
