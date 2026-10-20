import type { ReactNode } from 'react';
import { Plus } from 'lucide-react';
import type { SourceApiPort } from '../api/ports';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import type { PluginDetailSubject } from '../workbench/pluginDetailContext';
import { Button } from './Button';
import { PanelError } from './PanelPrimitives';
import { SourceEditorForm } from './sources/SourceEditorForm';
import { SourceList } from './sources/SourceList';
import { SourceHealthDrawer } from './sources/SourceHealthDrawer';
import type { SourceDetailFrames, SourceKindFormFrame } from './sources/sourceFrames';
import { useSourcesPanelState } from './sources/useSourcesPanelState';
import './SourcesPanel.css';

export interface SourcesPanelProps {
  sourceApi: SourceApiPort;
  /** Fired after a manual poll. sheetId is the source sheet, so the workspace can refresh/navigate. */
  onPolled?(sheetId: number | null): void;
  onOpenSourceHealthMainView?(sourceId: number): void;
  /** Opens the EXISTING Import workspace dialog
   *  (its "Feed" mode owns source creation — sources.spec.ts pins that
   *  SourcesPanel must not own a second creation form). Omitted -> no Add
   *  affordance renders, rather than a dead button. */
  onOpenImportDialog?(): void;
  sourceKindFormFrame?: SourceKindFormFrame;
  sourceDetailFrames?: SourceDetailFrames;
  /** Resolved sourceDetail contributions + the workspace dispatch for plugin
   *  detail tabs. */
  sourceDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?(
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ): ReactNode;
}

export function SourcesPanel({
  sourceApi,
  onPolled,
  onOpenSourceHealthMainView,
  onOpenImportDialog,
  sourceKindFormFrame,
  sourceDetailFrames,
  sourceDetailContributions,
  renderPluginDetailTab,
}: SourcesPanelProps) {
  const { state, actions } = useSourcesPanelState(sourceApi, onPolled);
  const {
    items,
    runs,
    runPages,
    expanded,
    form,
    busy,
    armedDelete,
    healthOpenId,
    healthLoading,
    health,
    healthError,
    error,
  } = state;
  const healthSource = healthOpenId == null ? null : items?.find((item) => item.id === healthOpenId) ?? null;
  const openHealth = healthOpenId == null ? null : health[healthOpenId] ?? null;

  return (
    <section className="content-stack" data-testid="sources">
      <div className="content-stack" data-testid="sources-panel">
        {onOpenImportDialog && (
          <div className="control-row" data-align="end">
            <Button
              type="button"
              className="btn-primary"
              data-testid="sources-add-source"
              onClick={onOpenImportDialog}
            >
              <Plus size={13} aria-hidden /> Add source
            </Button>
          </div>
        )}

        {error && (
          <PanelError testId="sources-error">{error}</PanelError>
        )}

        {form.editingId != null && (
          <SourceEditorForm
            actions={actions}
            busy={busy}
            form={form}
            formFrame={sourceKindFormFrame}
          />
        )}

        <SourceList
          actions={actions}
          armedDelete={armedDelete}
          busy={busy}
          expanded={expanded}
          items={items}
          runPages={runPages}
          runs={runs}
        />

        <SourceHealthDrawer
          actions={actions}
          busy={busy}
          healthError={healthError}
          healthLoading={healthLoading}
          healthOpenId={healthOpenId}
          healthSource={healthSource}
          openHealth={openHealth}
          onOpenSourceHealthMainView={onOpenSourceHealthMainView}
          sourceDetailFrames={sourceDetailFrames}
          sourceDetailContributions={sourceDetailContributions}
          renderPluginDetailTab={renderPluginDetailTab}
        />
      </div>
    </section>
  );
}
