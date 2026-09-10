import { useCallback, useReducer } from 'react';
import { ChevronDown, ChevronRight, Download, RefreshCw } from 'lucide-react';
import {
  ApiError,
  type EmbeddingExportResult,
  type EmbeddingIndexSummary,
} from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import { REFRESH_NEEDED_CODES } from './constants';
import { StatusChip } from '../PanelPrimitives';
import { IndexPolicyDetail } from './IndexPolicyDetail';
import { LensList } from './LensList';
import { ShowSimilar } from './ShowSimilar';

interface EmbeddingIndexCardProps {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  busy: boolean;
  onRefresh(indexId: string, mode: 'incremental' | 'full'): void;
  onChanged(): void | Promise<void>;
  onSelectSheet(sheetId: string): void;
}

interface CardState {
  showPolicy: boolean;
  lensVersion: number;
}

type CardAction = { type: 'toggle_policy' } | { type: 'lens_saved' };

const initialCardState: CardState = {
  showPolicy: false,
  lensVersion: 0,
};

function indexPolicyResetKey(index: EmbeddingIndexSummary): string {
  return [
    index.indexId,
    index.maintenanceMode,
    index.schedule ?? '',
    String(index.allowRemote),
    String(index.allowRemoteAutomaticRefresh),
    index.maxCostUsdPerRefresh == null ? '' : String(index.maxCostUsdPerRefresh),
    String(index.policyNeedsRepair),
  ].join(':');
}

function cardReducer(state: CardState, action: CardAction): CardState {
  switch (action.type) {
    case 'toggle_policy':
      return { ...state, showPolicy: !state.showPolicy };
    case 'lens_saved':
      return { ...state, lensVersion: state.lensVersion + 1 };
    default:
      return state;
  }
}

export function EmbeddingIndexCard({
  apiPort,
  index,
  busy,
  onRefresh,
  onChanged,
  onSelectSheet,
}: EmbeddingIndexCardProps) {
  const [state, dispatch] = useReducer(cardReducer, initialCardState);
  const onLensSaved = useCallback(() => dispatch({ type: 'lens_saved' }), []);

  return (
    <div className="embedding-index" data-testid={`embedding-index-${index.indexId}`}>
      <div className="embedding-index-head">
        <span className="embedding-index-name">{index.name}</span>
        {index.remote && (
          <StatusChip tone="info" size="sm" uppercase testId={`embedding-remote-${index.indexId}`}>
            remote
          </StatusChip>
        )}
        {index.refreshNeeded && (
          <StatusChip
            tone="error"
            size="sm"
            uppercase
            testId={`embedding-refresh-needed-${index.indexId}`}
          >
            refresh needed
          </StatusChip>
        )}
      </div>
      <p className="form-hint embedding-index-meta">
        {index.providerId} · {index.modelId}
      </p>
      <p className="form-hint" data-testid={`embedding-status-${index.indexId}`}>
        {index.readyItems}/{index.totalItems} ready
        {index.missingItems > 0 ? ` · ${index.missingItems} missing` : ''}
        {index.staleItems > 0 ? ` · ${index.staleItems} stale` : ''}
        {index.errorItems > 0 ? ` · ${index.errorItems} error` : ''}
      </p>
      <div className="embedding-index-actions">
        <button
          type="button"
          className="mini-btn"
          data-testid={`embedding-refresh-${index.indexId}`}
          disabled={busy}
          onClick={() => onRefresh(index.indexId, 'incremental')}
        >
          <RefreshCw size={11} /> {busy ? 'Refreshing…' : 'Refresh'}
        </button>
        <button
          type="button"
          className="mini-btn"
          data-testid={`embedding-rebuild-${index.indexId}`}
          disabled={busy}
          onClick={() => onRefresh(index.indexId, 'full')}
        >
          Rebuild
        </button>
      </div>
      <IndexAnalysis apiPort={apiPort} index={index} onSelectSheet={onSelectSheet} />
      <ExportControl apiPort={apiPort} index={index} />
      <ShowSimilar
        apiPort={apiPort}
        index={index}
        onRefresh={onRefresh}
        busy={busy}
        onLensSaved={onLensSaved}
      />
      <LensList apiPort={apiPort} index={index} reloadKey={state.lensVersion} />
      <button
        type="button"
        className="mini-btn embedding-policy-toggle"
        data-testid={`embedding-policy-toggle-${index.indexId}`}
        aria-expanded={state.showPolicy}
        onClick={() => dispatch({ type: 'toggle_policy' })}
      >
        {state.showPolicy ? <ChevronDown size={11} /> : <ChevronRight size={11} />}{' '}
        Details / Policy
      </button>
      {state.showPolicy && (
        <IndexPolicyDetail
          apiPort={apiPort}
          key={indexPolicyResetKey(index)}
          index={index}
          onChanged={onChanged}
        />
      )}
    </div>
  );
}

interface AnalysisState {
  k: string;
  seed: string;
  pcaSheetName: string;
  clusterSheetName: string;
  running: null | 'pca' | 'cluster';
  error: string | null;
}

type AnalysisAction =
  | { type: 'set_k'; k: string }
  | { type: 'set_seed'; seed: string }
  | { type: 'set_pca_sheet_name'; name: string }
  | { type: 'set_cluster_sheet_name'; name: string }
  | { type: 'run_start'; running: 'pca' | 'cluster' }
  | { type: 'set_error'; message: string | null }
  | { type: 'run_done' };

const initialAnalysisState = (indexName: string): AnalysisState => ({
  k: '5',
  seed: '0',
  pcaSheetName: `PCA of ${indexName}`,
  clusterSheetName: `Clusters of ${indexName}`,
  running: null,
  error: null,
});

function analysisReducer(state: AnalysisState, action: AnalysisAction): AnalysisState {
  switch (action.type) {
    case 'set_k':
      return { ...state, k: action.k };
    case 'set_seed':
      return { ...state, seed: action.seed };
    case 'set_pca_sheet_name':
      return { ...state, pcaSheetName: action.name };
    case 'set_cluster_sheet_name':
      return { ...state, clusterSheetName: action.name };
    case 'run_start':
      return { ...state, running: action.running, error: null };
    case 'set_error':
      return { ...state, error: action.message };
    case 'run_done':
      return { ...state, running: null };
    default:
      return state;
  }
}

function IndexAnalysis({
  apiPort,
  index,
  onSelectSheet,
}: {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  onSelectSheet(sheetId: string): void;
}) {
  const [state, dispatch] = useReducer(analysisReducer, index.name, initialAnalysisState);
  const busy = state.running !== null;

  const friendlyError = useCallback((error: unknown): string => {
    if (error instanceof ApiError) {
      if (error.code === 'embedding_analysis_insufficient_rows') {
        return 'Not enough embedded rows for this analysis — refresh the index or lower k.';
      }
      if (REFRESH_NEEDED_CODES.has(error.code ?? '')) {
        return 'Embeddings are out of date — refresh the index before running analysis.';
      }
    }
    return error instanceof Error ? error.message : String(error);
  }, []);

  const runPca = useCallback(async () => {
    if (!state.pcaSheetName.trim()) return;
    dispatch({ type: 'run_start', running: 'pca' });
    try {
      const result = await apiPort.runEmbeddingIndexAnalysis({
        action_id: 'embedding.index_project',
        sheet_name: state.pcaSheetName.trim(),
        params: {
          index_id: index.indexId,
          method: 'pca',
          dimensions: 2,
        },
      });
      onSelectSheet(String(result.sheetId));
    } catch (error) {
      dispatch({ type: 'set_error', message: friendlyError(error) });
    } finally {
      dispatch({ type: 'run_done' });
    }
  }, [apiPort, index.indexId, state.pcaSheetName, onSelectSheet, friendlyError]);

  const runCluster = useCallback(async () => {
    if (!state.clusterSheetName.trim()) return;
    const kValue = Number(state.k);
    if (!Number.isInteger(kValue) || kValue < 2) {
      dispatch({
        type: 'set_error',
        message: 'Enter a whole number k of at least 2 to cluster.',
      });
      return;
    }
    const seedValue = Number(state.seed);
    dispatch({ type: 'run_start', running: 'cluster' });
    try {
      const result = await apiPort.runEmbeddingIndexAnalysis({
        action_id: 'embedding.index_cluster',
        sheet_name: state.clusterSheetName.trim(),
        params: {
          index_id: index.indexId,
          method: 'kmeans',
          k: kValue,
          seed: Number.isFinite(seedValue) ? seedValue : 0,
        },
      });
      onSelectSheet(String(result.sheetId));
    } catch (error) {
      dispatch({ type: 'set_error', message: friendlyError(error) });
    } finally {
      dispatch({ type: 'run_done' });
    }
  }, [apiPort, index.indexId, state.clusterSheetName, state.k, state.seed, onSelectSheet, friendlyError]);

  return (
    <div className="embedding-analysis" data-testid={`embedding-analysis-${index.indexId}`}>
      <span className="form-label">Analyze</span>
      <div className="embedding-analysis-actions">
        <label className="embedding-analysis-field">
          PCA sheet name
          <input className="form-input" value={state.pcaSheetName} disabled={busy}
            onChange={(event) => dispatch({ type: 'set_pca_sheet_name', name: event.target.value })} />
        </label>
        <button
          type="button"
          className="mini-btn"
          data-testid={`embedding-run-pca-${index.indexId}`}
          disabled={busy || !state.pcaSheetName.trim()}
          title="Project embeddings to 2D (PCA) in a new sheet"
          onClick={() => void runPca()}
        >
          {state.running === 'pca' ? 'Projecting…' : 'Run PCA projection'}
        </button>
      </div>
      <div className="embedding-analysis-cluster">
        <label className="embedding-analysis-field">
          Clustering sheet name
          <input className="form-input" value={state.clusterSheetName} disabled={busy}
            onChange={(event) => dispatch({ type: 'set_cluster_sheet_name', name: event.target.value })} />
        </label>
        <label className="embedding-analysis-field">
          k
          <input
            className="form-input"
            type="number"
            min="2"
            step="1"
            required
            data-testid={`embedding-cluster-k-${index.indexId}`}
            value={state.k}
            onChange={(event) => dispatch({ type: 'set_k', k: event.target.value })}
          />
        </label>
        <label className="embedding-analysis-field">
          seed
          <input
            className="form-input"
            type="number"
            step="1"
            data-testid={`embedding-cluster-seed-${index.indexId}`}
            value={state.seed}
            onChange={(event) =>
              dispatch({ type: 'set_seed', seed: event.target.value })
            }
          />
        </label>
        <button
          type="button"
          className="mini-btn"
          data-testid={`embedding-run-cluster-${index.indexId}`}
          disabled={busy || !state.clusterSheetName.trim()}
          title="Cluster embeddings (k-means) into a new sheet"
          onClick={() => void runCluster()}
        >
          {state.running === 'cluster' ? 'Clustering…' : 'Run k-means'}
        </button>
      </div>
      {state.error && (
        <p className="form-error" data-testid={`embedding-analysis-error-${index.indexId}`}>
          {state.error}
        </p>
      )}
    </div>
  );
}

interface ExportState {
  busy: boolean;
  result: EmbeddingExportResult | null;
  error: string | null;
}

type ExportAction =
  | { type: 'start' }
  | { type: 'success'; result: EmbeddingExportResult }
  | { type: 'error'; message: string };

const initialExportState: ExportState = {
  busy: false,
  result: null,
  error: null,
};

function exportReducer(state: ExportState, action: ExportAction): ExportState {
  switch (action.type) {
    case 'start':
      return { ...state, busy: true, error: null };
    case 'success':
      return { ...state, busy: false, result: action.result };
    case 'error':
      return { ...state, busy: false, error: action.message, result: null };
    default:
      return state;
  }
}

function ExportControl({
  apiPort,
  index,
}: {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
}) {
  const [state, dispatch] = useReducer(exportReducer, initialExportState);

  const run = useCallback(async () => {
    dispatch({ type: 'start' });
    try {
      dispatch({
        type: 'success',
        result: await apiPort.exportEmbeddingIndex(index.indexId, {
          formats: ['jsonl', 'parquet'],
          includeVectors: true,
        }),
      });
    } catch (error) {
      if (error instanceof ApiError && REFRESH_NEEDED_CODES.has(error.code ?? '')) {
        dispatch({
          type: 'error',
          message: 'Embeddings are out of date — refresh before exporting.',
        });
      } else {
        dispatch({
          type: 'error',
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }, [apiPort, index.indexId]);

  return (
    <div className="embedding-export" data-testid={`embedding-export-${index.indexId}`}>
      <button
        type="button"
        className="mini-btn"
        data-testid={`embedding-export-run-${index.indexId}`}
        disabled={state.busy || index.refreshNeeded}
        title={
          index.refreshNeeded
            ? 'Refresh embeddings before exporting'
            : 'Export originals + vectors (JSONL + Parquet)'
        }
        onClick={() => void run()}
      >
        <Download size={11} /> {state.busy ? 'Exporting…' : 'Export'}
      </button>
      {state.error && <p className="form-error">{state.error}</p>}
      {state.result && (
        <ul
          className="embedding-export-artifacts"
          data-testid={`embedding-export-artifacts-${index.indexId}`}
        >
          {state.result.artifacts.map((artifact) => (
            <li key={`${artifact.format}:${artifact.sha256}`} className="form-hint">
              <a
                href={apiPort.embeddingExportArtifactUrl(index.indexId, artifact.format)}
                download
                data-testid={`embedding-export-download-${index.indexId}-${artifact.format}`}
              >
                {artifact.format}
              </a>
              : {artifact.rowCount} rows · {artifact.sha256.slice(0, 16)}…
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
