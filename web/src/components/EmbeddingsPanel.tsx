import { Plus } from 'lucide-react';
import type { SheetMeta } from '../api/open';
import type { EmbeddingApiPort } from '../api/ports';
import { CreateEmbeddingIndexDialog } from './embeddings/CreateEmbeddingIndexDialog';
import { EmbeddingIndexCard } from './embeddings/EmbeddingIndexCard';
import { OpenLensContext } from './embeddings/openLensContext';
import { useEmbeddingsPanelController } from './embeddings/useEmbeddingsPanelController';

export interface EmbeddingsPanelProps {
  apiPort: EmbeddingApiPort;
  sheet: SheetMeta;
  /** Open a sheet by id in the grid. Threaded App.selectSheet -> Sidebar ->
   *  EmbeddingsPanel so an analysis action (PCA / k-means) can navigate to the
   *  child sheet it mints. */
  onSelectSheet(sheetId: string): void;
  /** Open a saved lens AS A GRID VIEW. The LensList "Open"
   *  button delegates to App, which resolves the lens and pages the MAIN grid to
   *  its ranked row-set - opening a lens affects the grid, not just this list. */
  onOpenLens(lensId: number, name: string): void;
}

export function EmbeddingsPanel({
  apiPort,
  sheet,
  onSelectSheet,
  onOpenLens,
}: EmbeddingsPanelProps) {
  const panel = useEmbeddingsPanelController({ apiPort, sheet });

  return (
    <OpenLensContext.Provider value={onOpenLens}>
      <section className="sidebar-embeddings" data-testid="embeddings">
        <div className="embeddings-body" data-testid="embeddings-panel">
          {panel.error && (
            <div className="form-error" data-testid="embeddings-error" role="alert">
              {panel.error}
            </div>
          )}

          {panel.indexes && panel.indexes.length === 0 && !panel.adding && (
            <p className="form-hint" data-testid="embeddings-empty">
              No embedding indexes for this sheet yet.
            </p>
          )}

          {panel.indexes?.map((index) => (
            <EmbeddingIndexCard
              apiPort={apiPort}
              key={index.indexId}
              index={index}
              busy={panel.busyId === index.indexId}
              onRefresh={panel.refresh}
              onChanged={panel.load}
              onSelectSheet={onSelectSheet}
            />
          ))}

          <button
            type="button"
            className="mini-btn embeddings-add"
            data-testid="embeddings-add-button"
            onClick={() => void panel.openCreate()}
          >
            <Plus size={12} aria-hidden /> Create embedding index
          </button>
        </div>
        {panel.adding && (
          <CreateEmbeddingIndexDialog
            apiPort={apiPort}
            sheet={sheet}
            providers={panel.providers}
            form={panel.form}
            selectedCard={panel.selectedCard}
            remoteSelected={panel.remoteSelected}
            autoRefreshOn={panel.autoRefreshOn}
            createReady={panel.createReady}
            creating={panel.creating}
            onClose={panel.closeCreate}
            onSubmit={panel.submitCreate}
            onModelSelect={panel.selectModel}
            onSourceColumnToggle={panel.toggleSourceColumn}
            onAllowRemoteChange={panel.setAllowRemote}
            onAllowAutoRefreshChange={panel.setAllowAutoRefresh}
            onMaxCostChange={panel.setMaxCost}
            onConfirmRemoteChange={panel.setConfirmRemote}
          />
        )}
      </section>
    </OpenLensContext.Provider>
  );
}
