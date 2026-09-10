import { useEffect, useMemo, useState, type FormEvent, type MouseEvent } from 'react';
import { Boxes, CheckCircle2, X } from 'lucide-react';
import type { EmbeddingProvider, SheetMeta } from '../../api/types';
import type { EmbeddingApiPort } from '../../api/ports';
import { formatUsd } from '../../format';
import type { CreateEmbeddingIndexForm } from './createForm';
import {
  EMBEDDING_COST_SAMPLE_ROWS,
  estimateEmbeddingCost,
  type EmbeddingCostEstimate,
} from './costEstimate';
import { EmbeddingModelPicker } from './EmbeddingModelPicker';

interface CreateEmbeddingIndexDialogProps {
  apiPort: Pick<EmbeddingApiPort, 'getSheetData'>;
  sheet: SheetMeta;
  providers: EmbeddingProvider[] | null;
  form: CreateEmbeddingIndexForm;
  selectedCard: EmbeddingProvider | null;
  remoteSelected: boolean;
  autoRefreshOn: boolean;
  createReady: boolean;
  creating: boolean;
  onClose(): void;
  onSubmit(event: FormEvent<HTMLFormElement>): void;
  onModelSelect(provider: string, model: string): void;
  onSourceColumnToggle(columnName: string, checked: boolean): void;
  onAllowRemoteChange(allowed: boolean): void;
  onAllowAutoRefreshChange(allowed: boolean): void;
  onMaxCostChange(value: string): void;
  onConfirmRemoteChange(confirmed: boolean): void;
}

type CreateScreen = 'source' | 'egress' | 'cost' | 'summary';

interface CreateStep {
  id: CreateScreen;
  label: string;
  testId: string;
}

const SOURCE_STEP: CreateStep = {
  id: 'source',
  label: 'Source',
  testId: 'embedding-create-step-source',
};
const EGRESS_STEP: CreateStep = {
  id: 'egress',
  label: 'Egress',
  testId: 'embedding-create-step-egress',
};
const COST_STEP: CreateStep = {
  id: 'cost',
  label: 'Cost',
  testId: 'embedding-create-step-cost',
};
const SUMMARY_STEP: CreateStep = {
  id: 'summary',
  label: 'Review',
  testId: 'embedding-create-step-summary',
};

function providerLabel(
  selectedCard: EmbeddingProvider | null,
  form: CreateEmbeddingIndexForm,
): string {
  if (selectedCard) return selectedCard.label;
  if (form.provider && form.model) return `${form.provider} / ${form.model}`;
  if (form.provider) return form.provider;
  return 'No model selected';
}

function columnsLabel(columns: string[]): string {
  if (columns.length === 0) return 'No source columns';
  return columns.join(', ');
}

function refreshPolicyLabel(
  remoteSelected: boolean,
  form: CreateEmbeddingIndexForm,
  selectedCard: EmbeddingProvider | null,
): string {
  if (!remoteSelected) return 'Manual refresh with local processing.';
  if (!form.allowRemoteAutomaticRefresh) {
    return 'Manual refresh. Remote refreshes will ask again before sending data.';
  }
  const maxCost = Number(form.maxCost);
  // An empty field is `Number('') === 0` and a junk one is NaN; neither is a
  // ceiling the user typed. Composing "pre-authorized up to no cost limit"
  // out of a blank input stated an unbounded standing spend authorization
  // the user never granted. No figure, no authorization sentence.
  if (!(maxCost > 0)) {
    return `Unattended remote refresh needs a per-refresh cost limit before ${selectedCard?.label ?? form.provider} is pre-authorized.`;
  }
  return `Unattended remote refresh is pre-authorized up to ${formatUsd(maxCost)} per refresh with ${selectedCard?.label ?? form.provider}.`;
}

export function CreateEmbeddingIndexDialog({
  apiPort,
  sheet,
  providers,
  form,
  selectedCard,
  remoteSelected,
  autoRefreshOn,
  createReady,
  creating,
  onClose,
  onSubmit,
  onModelSelect,
  onSourceColumnToggle,
  onAllowRemoteChange,
  onAllowAutoRefreshChange,
  onMaxCostChange,
  onConfirmRemoteChange,
}: CreateEmbeddingIndexDialogProps) {
  const [step, setStep] = useState<CreateScreen>('source');
  const [costQuote, setCostQuote] = useState<{
    key: string;
    estimate: EmbeddingCostEstimate | null;
  } | null>(null);
  const steps = useMemo(
    () =>
      remoteSelected
        ? [SOURCE_STEP, EGRESS_STEP, COST_STEP, SUMMARY_STEP]
        : [SOURCE_STEP, SUMMARY_STEP],
    [remoteSelected],
  );
  const activeStep = steps.some((candidate) => candidate.id === step) ? step : 'source';

  const columns = sheet.columns;
  const currentStepIndex = Math.max(
    0,
    steps.findIndex((candidate) => candidate.id === activeStep),
  );
  const maxCostValue = Number(form.maxCost);
  const sourceReady =
    form.sourceColumns.length > 0 &&
    Boolean(form.provider) &&
    form.model.trim().length > 0 &&
    (selectedCard == null ||
      (Boolean(selectedCard.available) && selectedCard.modalityCompatible));
  const egressReady = !remoteSelected || form.allowRemote;
  const costReady =
    !remoteSelected ||
    !form.allowRemoteAutomaticRefresh ||
    (form.confirmRemote && maxCostValue > 0);
  const canAdvance =
    activeStep === 'source' ? sourceReady : activeStep === 'egress' ? egressReady : costReady;
  const finalStep = activeStep === 'summary';
  const modelSummary = providerLabel(selectedCard, form);
  const refreshSummary = refreshPolicyLabel(remoteSelected, form, selectedCard);
  const inputRate = selectedCard?.pricing?.inputUsdPerMillionTokens ?? null;
  const costQuoteKey = `${sheet.id}:${sheet.rowCount}:${form.sourceColumns.join('\u0000')}:${inputRate ?? 'unknown'}`;
  const costEstimateEligible = remoteSelected && inputRate != null && form.sourceColumns.length > 0;
  const visibleCostQuote = costQuote?.key === costQuoteKey ? costQuote : null;
  const costEstimate = visibleCostQuote?.estimate ?? null;
  const costEstimateLoading = costEstimateEligible && visibleCostQuote == null;

  useEffect(() => {
    let cancelled = false;
    if (!costEstimateEligible || inputRate == null) {
      return () => {
        cancelled = true;
      };
    }
    const load = async () => {
      try {
        const limit = Math.min(EMBEDDING_COST_SAMPLE_ROWS, sheet.rowCount);
        const page = await (limit > 0
          ? apiPort.getSheetData(String(sheet.id), 0, limit)
          : Promise.resolve({ rows: [] }));
        if (cancelled) return;
        setCostQuote({
          key: costQuoteKey,
          estimate: estimateEmbeddingCost({
            rows: page.rows,
            sheet,
            sourceColumns: form.sourceColumns,
            inputUsdPerMillionTokens: inputRate,
          }),
        });
      } catch {
        if (!cancelled) setCostQuote({ key: costQuoteKey, estimate: null });
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [apiPort, costEstimateEligible, costQuoteKey, form.sourceColumns, inputRate, sheet]);

  const goNext = () => {
    if (!canAdvance) return;
    const next = steps[Math.min(currentStepIndex + 1, steps.length - 1)];
    setStep(next.id);
  };

  const handleContinue = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    goNext();
  };

  const goBack = () => {
    const previous = steps[Math.max(currentStepIndex - 1, 0)];
    setStep(previous.id);
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    if (!finalStep) {
      event.preventDefault();
      return;
    }
    onSubmit(event);
  };

  return (
    <div className="modal-backdrop" data-testid="embeddings-create-modal">
      <form
        className="modal-card embeddings-create-card embeddings-form"
        data-testid="embeddings-form"
        onSubmit={handleSubmit}
      >
        <div className="modal-title">
          <Boxes size={15} aria-hidden />
          Create embedding index
          <button
            type="button"
            className="icon-btn embeddings-modal-close"
            aria-label="Close embedding index dialog"
            onClick={onClose}
          >
            <X size={15} />
          </button>
        </div>

        <div className="embeddings-create-stages" aria-label="Embedding index steps">
          {steps.map((item, index) => (
            <div
              key={item.id}
              className={`embeddings-create-stage${
                item.id === activeStep ? ' embeddings-create-stage-active' : ''
              }`}
              data-testid={item.testId}
              aria-current={item.id === activeStep ? 'step' : undefined}
            >
              {currentStepIndex > index ? (
                <CheckCircle2 size={14} aria-hidden />
              ) : (
                <span>{index + 1}</span>
              )}
              {item.label}
            </div>
          ))}
        </div>

        {activeStep === 'source' && (
          <div className="embeddings-create-panel" data-testid="embedding-create-panel-source">
            <div>
              <span className="form-label">Provider / model</span>
              <EmbeddingModelPicker
                models={providers ?? []}
                provider={form.provider}
                model={form.model}
                onSelect={onModelSelect}
              />
            </div>

            <div>
              <span className="form-label">Source columns</span>
              <div className="embeddings-columns" data-testid="embedding-source-columns">
                {columns.map((col) => (
                  <label key={col.id} className="embeddings-col-check">
                    <input
                      type="checkbox"
                      data-testid={`embedding-source-col-${col.name}`}
                      checked={form.sourceColumns.includes(col.name)}
                      onChange={(e) => onSourceColumnToggle(col.name, e.target.checked)}
                    />
                    {col.name}
                  </label>
                ))}
              </div>
            </div>
          </div>
        )}

        {activeStep === 'egress' && remoteSelected && (
          <div
            className="embeddings-create-panel embeddings-remote-controls"
            data-testid="embedding-remote-controls"
          >
            <p className="form-hint embeddings-egress-warning">
              {selectedCard?.label} is a remote provider. Your source text leaves this machine.
            </p>
            <label className="embeddings-col-check">
              <input
                type="checkbox"
                data-testid="embedding-allow-remote"
                checked={form.allowRemote}
                onChange={(e) => onAllowRemoteChange(e.target.checked)}
              />
              Allow sending data to {selectedCard?.label}
            </label>
          </div>
        )}

        {activeStep === 'cost' && remoteSelected && (
          <div
            className="embeddings-create-panel embeddings-cost-controls"
            data-testid="embedding-cost-controls"
          >
            <div className="embeddings-cost-estimate" data-testid="embedding-cost-estimate">
              <span className="form-label">Estimated first run</span>
              {costEstimateLoading ? (
                <p className="form-hint" data-testid="embedding-cost-estimate-loading">
                  Estimating selected-column usage…
                </p>
              ) : costEstimate ? (
                <>
                  <strong data-testid="embedding-cost-estimate-first-run">
                    {formatUsd(costEstimate.firstRunUsd)} for {sheet.rowCount.toLocaleString()} rows
                  </strong>
                  <p className="form-hint" data-testid="embedding-cost-estimate-per-100">
                    {formatUsd(costEstimate.per100RowsUsd)} per 100 rows · about{' '}
                    {costEstimate.estimatedTokens.toLocaleString()} input tokens
                  </p>
                  <p className="form-hint">
                    Based on {costEstimate.sampledRows.toLocaleString()} sampled rows and a{' '}
                    {formatUsd(inputRate ?? 0)} per 1M-token provider list price.
                    {selectedCard?.pricing?.sourceUrl && (
                      <>{' '}<a href={selectedCard.pricing.sourceUrl} target="_blank" rel="noreferrer">Pricing source</a>.</>
                    )}{' '}Actual tokenization and charges may vary.
                  </p>
                </>
              ) : (
                <p className="form-hint" data-testid="embedding-cost-estimate-unavailable">
                  No unit price is available for this model, so Frisket cannot estimate the first run.
                </p>
              )}
            </div>
            <label className="embeddings-col-check">
              <input
                type="checkbox"
                data-testid="embedding-allow-auto-refresh"
                disabled={!form.allowRemote}
                checked={form.allowRemoteAutomaticRefresh}
                onChange={(e) => onAllowAutoRefreshChange(e.target.checked)}
              />
              Pre-authorize unattended remote refresh
            </label>
            <p className="form-hint" data-testid="embedding-auto-refresh-note">
              This only pre-authorizes cost + data egress so a future scheduled
              or on-new-rows refresh can run without re-confirming. It does not
              enable a schedule here. Refresh stays manual until you set one.
            </p>
            {autoRefreshOn && (
              <div className="embeddings-auto-confirm" data-testid="embedding-auto-confirm">
                <label className="form-label" htmlFor="embedding-max-cost">
                  Max cost per refresh (USD)
                </label>
                <input
                  id="embedding-max-cost"
                  className="form-input"
                  type="number"
                  min="0"
                  step="0.01"
                  data-testid="embedding-max-cost"
                  value={form.maxCost}
                  onChange={(e) => onMaxCostChange(e.target.value)}
                />
                <label className="embeddings-col-check">
                  <input
                    type="checkbox"
                    data-testid="embedding-remote-confirm"
                    checked={form.confirmRemote}
                    onChange={(e) => onConfirmRemoteChange(e.target.checked)}
                  />
                  I understand an unattended remote refresh sends data externally and may incur cost.
                </label>
              </div>
            )}
          </div>
        )}

        {activeStep === 'summary' && (
          <div className="embeddings-create-panel" data-testid="embedding-create-summary">
            <dl className="embeddings-create-summary-list">
              <div>
                <dt>Rows</dt>
                <dd>{sheet.rowCount.toLocaleString()} rows in this sheet</dd>
              </div>
              <div>
                <dt>Columns</dt>
                <dd data-testid="embedding-create-summary-columns">
                  {columnsLabel(form.sourceColumns)}
                </dd>
              </div>
              <div>
                <dt>Model</dt>
                <dd>{modelSummary}</dd>
              </div>
              <div>
                <dt>Remote egress</dt>
                <dd>
                  {remoteSelected
                    ? `Allowed for ${selectedCard?.label ?? form.provider}`
                    : 'None'}
                </dd>
              </div>
              <div>
                <dt>Refresh policy</dt>
                <dd data-testid="embedding-create-summary-refresh">{refreshSummary}</dd>
              </div>
            </dl>
          </div>
        )}

        <div className="embeddings-form-actions">
          <button type="button" className="mini-btn" onClick={onClose}>
            Cancel
          </button>
          {currentStepIndex > 0 && (
            <button
              type="button"
              className="mini-btn"
              data-testid="embedding-create-back"
              onClick={goBack}
            >
              Back
            </button>
          )}
          {!finalStep ? (
            <button
              type="button"
              className="btn btn-primary"
              data-testid="embedding-create-next"
              disabled={!canAdvance}
              onClick={handleContinue}
            >
              Continue
            </button>
          ) : (
            <button
              type="submit"
              className="btn btn-primary"
              data-testid="embedding-create"
              disabled={!createReady || creating}
            >
              {creating ? 'Creating...' : 'Create index'}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
