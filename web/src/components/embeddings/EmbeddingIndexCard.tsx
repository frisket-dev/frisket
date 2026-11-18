import '../EmbeddingsPanel.css';
import { Button } from '../Button';
import { TextInput } from '../FormControls';
import { useCallback, useState } from 'react';
import { ChevronDown, ChevronRight, Download, RefreshCw, ScanSearch } from 'lucide-react';
import {
  ApiError,
  type EmbeddingExportResult,
  type EmbeddingIndexSummary,
  type SheetMeta,
} from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import { REFRESH_NEEDED_CODES } from './constants';
import { StatusChip } from '../PanelPrimitives';
import { IndexPolicyDetail } from './IndexPolicyDetail';
import { ViewList } from './ViewList';
import { errorMessage } from '../../hooks/errorMessage';
import { useAction } from '../../hooks/useAction';

interface EmbeddingIndexCardProps {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  busy: boolean;
  onRefresh(indexId: string, mode: 'incremental' | 'full'): void;
  onChanged(): void | Promise<void>;
  onSelectSheet(sheetId: string): void;
  sheet: SheetMeta;
  selectedRowIds: readonly string[];
  onFindSimilar(input: {
    sheetId: string;
    rowId: string;
    columnId: string;
    label: string;
    namedIndexId?: string;
  }): void;
}

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

export function EmbeddingIndexCard({
  apiPort,
  index,
  busy,
  onRefresh,
  onChanged,
  onSelectSheet,
  sheet,
  selectedRowIds,
  onFindSimilar,
}: EmbeddingIndexCardProps) {
  const [showPolicy, setShowPolicy] = useState(false);
  const selectedRowId = selectedRowIds.length === 1 ? selectedRowIds[0] : null;
  const sourceColumn = sheet.columns.find((column) =>
    index.sourceColumns.includes(column.name),
  );
  const canFindSimilar = selectedRowId !== null && sourceColumn !== undefined;

  return (
    <div className="panel-card" data-testid={`embedding-index-${index.indexId}`}>
      <div className="control-row">
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
      <div className="control-row">
        <Button
          type="button"

          data-testid={`embedding-refresh-${index.indexId}`}
          disabled={busy}
          onClick={() => onRefresh(index.indexId, 'incremental')}
        >
          <RefreshCw size={11} /> {busy ? 'Refreshing…' : 'Refresh'}
        </Button>
        <Button
          type="button"

          data-testid={`embedding-rebuild-${index.indexId}`}
          disabled={busy}
          onClick={() => onRefresh(index.indexId, 'full')}
        >
          Rebuild
        </Button>
      </div>
      <IndexAnalysis apiPort={apiPort} index={index} onSelectSheet={onSelectSheet} />
      <ExportControl apiPort={apiPort} index={index} />
      <Button
        type="button"

        data-testid={`embedding-find-similar-${index.indexId}`}
        disabled={!canFindSimilar}
        title={canFindSimilar
          ? 'Find rows similar to the selected row'
          : 'Select one row to find similar rows'}
        onClick={() => {
          if (!selectedRowId || !sourceColumn) return;
          onFindSimilar({
            sheetId: String(sheet.id),
            rowId: selectedRowId,
            columnId: String(sourceColumn.id),
            label: `${index.name}, row ${selectedRowId}`,
            namedIndexId: index.indexId,
          });
        }}
      >
        <ScanSearch size={11} /> Find similar
      </Button>
      <ViewList apiPort={apiPort} index={index} reloadKey={0} />
      <Button
        type="button"
        className="embedding-policy-toggle"
        data-testid={`embedding-policy-toggle-${index.indexId}`}
        aria-expanded={showPolicy}
        onClick={() => setShowPolicy((shown) => !shown)}
      >
        {showPolicy ? <ChevronDown size={11} /> : <ChevronRight size={11} />}{' '}
        Details / Policy
      </Button>
      {showPolicy && (
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

function IndexAnalysis({
  apiPort,
  index,
  onSelectSheet,
}: {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
  onSelectSheet(sheetId: string): void;
}) {
  const [k, setK] = useState('5');
  const [seed, setSeed] = useState('0');
  const [pcaSheetName, setPcaSheetName] = useState(() => `PCA of ${index.name}`);
  const [clusterSheetName, setClusterSheetName] = useState(() => `Clusters of ${index.name}`);
  const [running, setRunning] = useState<null | 'pca' | 'cluster'>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = running !== null;

  const friendlyError = useCallback((error: unknown): string => {
    if (error instanceof ApiError) {
      if (error.code === 'embedding_analysis_insufficient_rows') {
        return 'Not enough embedded rows for this analysis — refresh the index or lower k.';
      }
      if (REFRESH_NEEDED_CODES.has(error.code ?? '')) {
        return 'Embeddings are out of date — refresh the index before running analysis.';
      }
    }
    return errorMessage(error);
  }, []);

  const runPca = useCallback(async () => {
    if (!pcaSheetName.trim()) return;
    setRunning('pca');
    setError(null);
    try {
      const result = await apiPort.runEmbeddingIndexAnalysis({
        action_id: 'embedding.index_project',
        sheet_name: pcaSheetName.trim(),
        params: {
          index_id: index.indexId,
          method: 'pca',
          dimensions: 2,
        },
      });
      onSelectSheet(String(result.sheetId));
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setRunning(null);
    }
  }, [apiPort, index.indexId, pcaSheetName, onSelectSheet, friendlyError]);

  const runCluster = useCallback(async () => {
    if (!clusterSheetName.trim()) return;
    const kValue = Number(k);
    if (!Number.isInteger(kValue) || kValue < 2) {
      setError('Enter a whole number k of at least 2 to cluster.');
      return;
    }
    const seedValue = Number(seed);
    setRunning('cluster');
    setError(null);
    try {
      const result = await apiPort.runEmbeddingIndexAnalysis({
        action_id: 'embedding.index_cluster',
        sheet_name: clusterSheetName.trim(),
        params: {
          index_id: index.indexId,
          method: 'kmeans',
          k: kValue,
          seed: Number.isFinite(seedValue) ? seedValue : 0,
        },
      });
      onSelectSheet(String(result.sheetId));
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setRunning(null);
    }
  }, [apiPort, index.indexId, clusterSheetName, k, seed, onSelectSheet, friendlyError]);

  return (
    <div className="embedding-analysis" data-testid={`embedding-analysis-${index.indexId}`}>
      <span className="form-label">Analyze</span>
      <div className="embedding-analysis-actions">
        <label className="embedding-analysis-field form-field">
          PCA sheet name
          <TextInput value={pcaSheetName} disabled={busy}
            onChange={(event) => setPcaSheetName(event.target.value)} />
        </label>
        <Button
          type="button"

          data-testid={`embedding-run-pca-${index.indexId}`}
          disabled={busy || !pcaSheetName.trim()}
          title="Project embeddings to 2D (PCA) in a new sheet"
          onClick={() => void runPca()}
        >
          {running === 'pca' ? 'Projecting…' : 'Run PCA projection'}
        </Button>
      </div>
      <div className="embedding-analysis-cluster">
        <label className="embedding-analysis-field form-field">
          Clustering sheet name
          <TextInput value={clusterSheetName} disabled={busy}
            onChange={(event) => setClusterSheetName(event.target.value)} />
        </label>
        <label className="embedding-analysis-field form-field">
          k
          <TextInput
            type="number"
            min="2"
            step="1"
            required
            data-testid={`embedding-cluster-k-${index.indexId}`}
            value={k}
            onChange={(event) => setK(event.target.value)}
          />
        </label>
        <label className="embedding-analysis-field form-field">
          seed
          <TextInput
            type="number"
            step="1"
            data-testid={`embedding-cluster-seed-${index.indexId}`}
            value={seed}
            onChange={(event) => setSeed(event.target.value)}
          />
        </label>
        <Button
          type="button"

          data-testid={`embedding-run-cluster-${index.indexId}`}
          disabled={busy || !clusterSheetName.trim()}
          title="Cluster embeddings (k-means) into a new sheet"
          onClick={() => void runCluster()}
        >
          {running === 'cluster' ? 'Clustering…' : 'Run k-means'}
        </Button>
      </div>
      {error && (
        <p className="panel-error" data-testid={`embedding-analysis-error-${index.indexId}`}>
          {error}
        </p>
      )}
    </div>
  );
}

function ExportControl({
  apiPort,
  index,
}: {
  apiPort: EmbeddingApiPort;
  index: EmbeddingIndexSummary;
}) {
  const exportAction = useAction();
  const runExport = exportAction.run;
  const [result, setResult] = useState<EmbeddingExportResult | null>(null);

  const run = useCallback(async () => {
    const exported = await runExport(async () => {
      try {
        return await apiPort.exportEmbeddingIndex(index.indexId, {
          formats: ['jsonl', 'parquet'],
          includeVectors: true,
        });
      } catch (error) {
        if (error instanceof ApiError && REFRESH_NEEDED_CODES.has(error.code ?? '')) {
          throw new Error('Embeddings are out of date — refresh before exporting.', { cause: error });
        }
        throw error;
      }
    });
    setResult(exported ?? null);
  }, [apiPort, index.indexId, runExport]);

  return (
    <div className="content-stack" data-testid={`embedding-export-${index.indexId}`}>
      <Button
        type="button"

        data-testid={`embedding-export-run-${index.indexId}`}
        disabled={exportAction.busy || index.refreshNeeded}
        title={
          index.refreshNeeded
            ? 'Refresh embeddings before exporting'
            : 'Export originals + vectors (JSONL + Parquet)'
        }
        onClick={() => void run()}
      >
        <Download size={11} /> {exportAction.busy ? 'Exporting…' : 'Export'}
      </Button>
      {exportAction.error && <p className="panel-error">{exportAction.error}</p>}
      {result && (
        <ul
          className="panel-list"
          data-testid={`embedding-export-artifacts-${index.indexId}`}
        >
          {result.artifacts.map((artifact) => (
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
