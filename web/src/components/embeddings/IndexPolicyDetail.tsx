import { useCallback, useReducer } from 'react';
import type { EmbeddingIndexSummary } from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import { PanelSelect } from '../PanelSelect';

interface IndexPolicyDetailProps {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  onChanged(): void | Promise<void>;
}

interface PolicyState {
  mode: 'manual' | 'on_source_append' | 'scheduled';
  schedule: string;
  allowRemote: boolean;
  allowAuto: boolean;
  maxCost: string;
  busy: boolean;
  error: string | null;
}

type PolicyAction =
  | { type: 'set_mode'; mode: PolicyState['mode'] }
  | { type: 'set_schedule'; schedule: string }
  | { type: 'set_allow_remote'; allowRemote: boolean }
  | { type: 'set_allow_auto'; allowAuto: boolean }
  | { type: 'set_max_cost'; maxCost: string }
  | { type: 'save_start' }
  | { type: 'save_error'; message: string }
  | { type: 'save_done' };

function parseMaxCost(value: string): number | null | undefined {
  if (value.trim() === '') return null;
  const cost = Number(value);
  return Number.isFinite(cost) && cost > 0 ? cost : undefined;
}

function createInitialPolicyState(index: EmbeddingIndexSummary): PolicyState {
  return {
    mode: index.maintenanceMode,
    schedule: index.schedule ?? '',
    allowRemote: index.allowRemote,
    allowAuto: index.allowRemoteAutomaticRefresh,
    maxCost:
      index.maxCostUsdPerRefresh == null ? '' : String(index.maxCostUsdPerRefresh),
    busy: false,
    error: null,
  };
}

function policyReducer(state: PolicyState, action: PolicyAction): PolicyState {
  switch (action.type) {
    case 'set_mode':
      return { ...state, mode: action.mode };
    case 'set_schedule':
      return { ...state, schedule: action.schedule };
    case 'set_allow_remote':
      return { ...state, allowRemote: action.allowRemote };
    case 'set_allow_auto':
      return { ...state, allowAuto: action.allowAuto };
    case 'set_max_cost':
      return { ...state, maxCost: action.maxCost };
    case 'save_start':
      return { ...state, busy: true, error: null };
    case 'save_error':
      return { ...state, error: action.message };
    case 'save_done':
      return { ...state, busy: false };
    default:
      return state;
  }
}

export function IndexPolicyDetail({ apiPort, index, onChanged }: IndexPolicyDetailProps) {
  const freshness = index.freshness;
  const [state, dispatch] = useReducer(
    policyReducer,
    index,
    createInitialPolicyState,
  );

  // Honest gate surface: a remote index with automatic refresh on but NO cost cap
  // will still BLOCK at refresh time (embedding_cost_requires_confirmation). Saving
  // stores the preauthorization; it never means "automatic refresh is enabled".
  const maxCost = parseMaxCost(state.maxCost);
  const gateBlocked = index.remote && state.allowAuto && maxCost == null;

  const save = useCallback(async () => {
    const nextMaxCost = parseMaxCost(state.maxCost);
    if (nextMaxCost === undefined) {
      dispatch({
        type: 'save_error',
        message: 'Max $/refresh must be a positive finite number or blank.',
      });
      return;
    }
    dispatch({ type: 'save_start' });
    try {
      const allowAuto = state.allowRemote ? state.allowAuto : false;
      await apiPort.updateEmbeddingIndexPolicy({
        indexId: index.indexId,
        maintenancePolicy: {
          mode: state.mode,
          schedule: state.mode === 'scheduled' ? state.schedule.trim() : null,
        },
        providerPolicy: {
          allowRemote: state.allowRemote,
          allowRemoteAutomaticRefresh: allowAuto,
          maxCostUsdPerRefresh: nextMaxCost,
        },
      });
      await onChanged();
    } catch (error) {
      dispatch({
        type: 'save_error',
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      dispatch({ type: 'save_done' });
    }
  }, [apiPort, index.indexId, state, onChanged]);

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
      <label className="form-row">
        Maintenance
        <PanelSelect
          data-testid={`embedding-policy-mode-${index.indexId}`}
          value={state.mode}
          onChange={(event) =>
            dispatch({
              type: 'set_mode',
              mode: event.target.value as PolicyState['mode'],
            })
          }
        >
          <option value="manual">manual</option>
          <option value="on_source_append">on source append</option>
          <option value="scheduled">scheduled</option>
        </PanelSelect>
      </label>
      {state.mode === 'scheduled' && (
        <label className="form-row">
          Schedule
          <input
            data-testid={`embedding-policy-schedule-${index.indexId}`}
            value={state.schedule}
            placeholder="@hourly / @daily / 6h"
            onChange={(event) =>
              dispatch({ type: 'set_schedule', schedule: event.target.value })
            }
          />
        </label>
      )}
      {index.remote && (
        <>
          <label className="form-row">
            <input
              type="checkbox"
              data-testid={`embedding-policy-allow-remote-${index.indexId}`}
              checked={state.allowRemote}
              onChange={(event) =>
                dispatch({
                  type: 'set_allow_remote',
                  allowRemote: event.target.checked,
                })
              }
            />
            Allow remote egress
          </label>
          <label className="form-row">
            <input
              type="checkbox"
              data-testid={`embedding-policy-allow-auto-${index.indexId}`}
              checked={state.allowAuto}
              disabled={!state.allowRemote}
              onChange={(event) =>
                dispatch({
                  type: 'set_allow_auto',
                  allowAuto: event.target.checked,
                })
              }
            />
            Allow automatic remote refresh
          </label>
          <label className="form-row">
            Max $/refresh
            <input
              data-testid={`embedding-policy-max-cost-${index.indexId}`}
              value={state.maxCost}
              inputMode="decimal"
              placeholder="required for automatic"
              onChange={(event) =>
                dispatch({ type: 'set_max_cost', maxCost: event.target.value })
              }
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
      {state.error && <p className="form-error">{state.error}</p>}
      <button
        type="button"
        className="mini-btn"
        data-testid={`embedding-policy-save-${index.indexId}`}
        disabled={state.busy}
        onClick={() => void save()}
      >
        {state.busy ? 'Saving…' : 'Save policy'}
      </button>
    </div>
  );
}
