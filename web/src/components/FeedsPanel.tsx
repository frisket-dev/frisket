import type { ReactNode } from 'react';
import { Plus } from 'lucide-react';
import type { FeedApiPort } from '../api/ports';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import type { PluginDetailSubject } from '../workbench/pluginDetailContext';
import { Button } from './Button';
import { PanelError } from './PanelPrimitives';
import { FeedEditorForm } from './feeds/FeedEditorForm';
import { FeedList } from './feeds/FeedList';
import { FeedHealthDrawer } from './feeds/FeedHealthDrawer';
import type { FeedDetailFrames, FeedKindFormFrame } from './feeds/feedFrames';
import { useFeedsPanelState } from './feeds/useFeedsPanelState';
import './FeedsPanel.css';

export interface FeedsPanelProps {
  feedApi: FeedApiPort;
  /** Fired after a manual poll. sheetId is the source sheet, so the workspace can refresh/navigate. */
  onPolled?(sheetId: number | null): void;
  onOpenFeedHealthMainView?(feedId: number): void;
  /** Opens the EXISTING Import workspace dialog
   *  (its "Feed" mode owns source creation — sources.spec.ts pins that
   *  FeedsPanel must not own a second creation form). Omitted -> no Add
   *  affordance renders, rather than a dead button. */
  onOpenImportDialog?(): void;
  feedKindFormFrame?: FeedKindFormFrame;
  feedDetailFrames?: FeedDetailFrames;
  /** Resolved sourceDetail contributions + the workspace dispatch for plugin
   *  detail tabs. */
  feedDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?(
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ): ReactNode;
}

export function FeedsPanel({
  feedApi,
  onPolled,
  onOpenFeedHealthMainView,
  onOpenImportDialog,
  feedKindFormFrame,
  feedDetailFrames,
  feedDetailContributions,
  renderPluginDetailTab,
}: FeedsPanelProps) {
  const { state, actions } = useFeedsPanelState(feedApi, onPolled);
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
  const healthFeed = healthOpenId == null ? null : items?.find((item) => item.id === healthOpenId) ?? null;
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
              <Plus size={13} aria-hidden /> Add feed
            </Button>
          </div>
        )}

        {error && (
          <PanelError testId="sources-error">{error}</PanelError>
        )}

        {form.editingId != null && (
          <FeedEditorForm
            actions={actions}
            busy={busy}
            form={form}
            formFrame={feedKindFormFrame}
          />
        )}

        <FeedList
          actions={actions}
          armedDelete={armedDelete}
          busy={busy}
          expanded={expanded}
          items={items}
          runPages={runPages}
          runs={runs}
        />

        <FeedHealthDrawer
          actions={actions}
          busy={busy}
          healthError={healthError}
          healthLoading={healthLoading}
          healthOpenId={healthOpenId}
          healthFeed={healthFeed}
          openHealth={openHealth}
          onOpenFeedHealthMainView={onOpenFeedHealthMainView}
          feedDetailFrames={feedDetailFrames}
          feedDetailContributions={feedDetailContributions}
          renderPluginDetailTab={renderPluginDetailTab}
        />
      </div>
    </section>
  );
}
