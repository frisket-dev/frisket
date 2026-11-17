import '../PanelPrimitives.css';
import '../FormControls.css';
import { Button } from '../Button';
import { useCallback, useState } from 'react';
import type { EmbeddingIndexSummary } from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import { PanelSelect } from '../PanelSelect';
import { useAction } from '../../hooks/useAction';

interface IndexPolicyDetailProps {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  onChanged(): void | Promise<void>;
}

type MaintenanceMode = EmbeddingIndexSummary['maintenanceMode'];

function parseMaxCost(value: string): number | null | undefined {
  if (value.trim() === '') return null;
  const cost = Number(value);
  return Number.isFinite(cost) && cost > 0 ? cost : undefined;
}

export function IndexPolicyDetail({ apiPort, index, onChanged }: IndexPolicyDetailProps) {
  const freshness = index.freshness;
  const [mode, setMode] = useState<MaintenanceMode>(index.maintenanceMode);
  const [schedule, setSchedule] = useState(index.schedule ?? '');
  const [allowRemote, setAllowRemote] = useState(index.allowRemote);
  const [allowAuto, setAllowAuto] = useState(index.allowRemoteAutomaticRefresh);
  const [maxCostInput, setMaxCostInput] = useState(
    index.maxCostUsdPerRefresh == null ? '' : String(index.maxCostUsdPerRefresh),
  );
  const saveAction = useAction();
  const { run: runSave, setError: setSaveError } = saveAction;

  // Honest gate surface: a remote index with automatic refresh on but NO cost cap
  // will still BLOCK at refresh time (embedding_cost_requires_confirmation). Saving
  // stores the preauthorization; it never means "automatic refresh is enabled".
  const maxCost = parseMaxCost(maxCostInput);
  const gateBlocked = index.remote && allowAuto && maxCost == null;

  const save = useCallback(async () => {
    const nextMaxCost = parseMaxCost(maxCostInput);
    if (nextMaxCost === undefined) {
      setSaveError('Max $/refresh must be a positive finite number or blank.');
      return;
    }
    await runSave(async () => {
      await apiPort.updateEmbeddingIndexPolicy({
        indexId: index.indexId,
        maintenancePolicy: {
          mode,
          schedule: mode === 'scheduled' ? schedule.trim() : null,
        },
        providerPolicy: {
          allowRemote,
          allowRemoteAutomaticRefresh: allowRemote ? allowAuto : false,
          maxCostUsdPerRefresh: nextMaxCost,
        },
      });
      await onChanged();
    });
  }, [
    apiPort,
    index.indexId,
    maxCostInput,
    mode,
    schedule,
    allowRemote,
    allowAuto,
    onChanged,
    runSave,
    setSaveError,
  ]);

  return (
    <div className="embedding-policy" data-testid={`embedding-policy-${index.indexId}`}>
      <p className="form-hint" data-testid={`embedding-freshness-${index.indexId}`}>
        freshness: <strong>{freshness.reason}</strong> · {freshness.current} current
        {freshness.missing > 0 ? ` · ${freshness.missing} missing` : ''}
        {freshness.stale > 0 ? ` · ${freshness.stale} stale` : ''}
        {freshness.error > 0 ? ` · ${freshness.error} error` : ''}
      </p>
      <p className="form-hint" data-testid={`embedding-refresh-jobs-${index.indexId}`}>
        {freshness.pendingRefreshJobId != null
          ? `refresh queued (job ${freshness.pendingRefreshJobId})`
          : index.lastRefreshedAt
            ? `last refreshed ${index.lastRefreshedAt}${
                freshness.lastRefreshJobId != null
                  ? ` (job ${freshness.lastRefreshJobId})`
                  : ''
              }`
            : 'never refreshed'}
      </p>
      {index.policyNeedsRepair && (
        <p
          className="form-hint embedding-policy-repair-note"
          data-testid={`embedding-policy-repair-note-${index.indexId}`}
        >
          Stored policy values were invalid. Saving replaces them with safe values.
        </p>
      )}
      <label className="form-row form-field">
        Maintenance
        <PanelSelect
          data-testid={`embedding-policy-mode-${index.indexId}`}
          value={mode}
          onChange={(event) => setMode(event.target.value as MaintenanceMode)}
        >
          <option value="manual">manual</option>
          <option value="on_source_append">on source append</option>
          <option value="scheduled">scheduled</option>
        </PanelSelect>
      </label>
      {mode === 'scheduled' && (
        <label className="form-row">
          Schedule
          <input
            data-testid={`embedding-policy-schedule-${index.indexId}`}
            value={schedule}
            placeholder="@hourly / @daily / 6h"
            onChange={(event) => setSchedule(event.target.value)}
          />
        </label>
      )}
      {index.remote && (
        <>
          <label className="form-row">
            <input
              type="checkbox"
              data-testid={`embedding-policy-allow-remote-${index.indexId}`}
              checked={allowRemote}
              onChange={(event) => setAllowRemote(event.target.checked)}
            />
            Allow remote egress
          </label>
          <label className="form-row">
            <input
              type="checkbox"
              data-testid={`embedding-policy-allow-auto-${index.indexId}`}
              checked={allowAuto}
              disabled={!allowRemote}
              onChange={(event) => setAllowAuto(event.target.checked)}
            />
            Allow automatic remote refresh
          </label>
          <label className="form-row">
            Max $/refresh
            <input
              data-testid={`embedding-policy-max-cost-${index.indexId}`}
              value={maxCostInput}
              inputMode="decimal"
              placeholder="required for automatic"
              onChange={(event) => setMaxCostInput(event.target.value)}
            />
          </label>
          {gateBlocked && (
            <p
              className="form-hint embedding-policy-gate-note"
              data-testid={`embedding-policy-gate-note-${index.indexId}`}
            >
              Automatic refresh will still block (embedding_cost_requires_confirmation)
              until you set a positive cost cap. Saving stores the preauthorization only.
            </p>
          )}
        </>
      )}
      {saveAction.error && <p className="panel-error">{saveAction.error}</p>}
      <Button
        type="button"

        data-testid={`embedding-policy-save-${index.indexId}`}
        disabled={saveAction.busy}
        onClick={() => void save()}
      >
        {saveAction.busy ? 'Saving…' : 'Save policy'}
      </Button>
    </div>
  );
}
