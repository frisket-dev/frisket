import { useState } from 'react';
import type { ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';
import type { FeedHealth, FeedInfo } from '../../api/open';
import type { WorkbenchResolvedLayoutContribution } from '../../workbench/layout';
import type { PluginDetailSubject } from '../../workbench/pluginDetailContext';
import { Button, TabButton } from '../Button';
import { PanelEmpty, PanelError } from '../PanelPrimitives';
import { WindowCloseButton } from '../WindowCloseButton';
import { redactedUrl, feedConfigSummary, feedKindLabel } from './feedConfig';
import type { FeedDetailFrames } from './feedFrames';
import { FeedHealthSummary, FeedHealthChecks } from './FeedHealthContent';
import { formatCost, statusLabel } from './feedFormatting';
import type { BusyFeed } from './feedState';
import type { FeedPanelActions } from './useFeedsPanelState';
import { contributionSlug } from '../../workbench/visibility';

const PlainFeedDetailFrame = ({ children }: { children: ReactNode }) => <>{children}</>;
const PLAIN_FEED_DETAIL_FRAMES: FeedDetailFrames = {
  Summary: PlainFeedDetailFrame,
  Health: PlainFeedDetailFrame,
  Runs: PlainFeedDetailFrame,
};

const EMPTY_CONTRIBUTIONS: WorkbenchResolvedLayoutContribution[] = [];

// First-party source detail tab bindings: legacy short names (pinned testids)
// + which contribution frame wraps each tab button. The tab SET renders from
// resolved sourceDetail contributions — no hardcoded tab union.
const FIRST_PARTY_FEED_DETAIL_TABS: Record<
  string,
  { shortName: 'summary' | 'health' | 'runs'; frameKey: keyof FeedDetailFrames }
> = {
  'frisket.core.view.source_summary': { shortName: 'summary', frameKey: 'Summary' },
  'frisket.core.view.source_health': { shortName: 'health', frameKey: 'Health' },
  'frisket.core.view.source_runs': { shortName: 'runs', frameKey: 'Runs' },
};
interface FeedHealthDrawerProps {
  actions: FeedPanelActions;
  busy: BusyFeed;
  healthError: string | null;
  healthLoading: boolean;
  healthOpenId: number | null;
  healthFeed: FeedInfo | null;
  openHealth: FeedHealth | null;
  onOpenFeedHealthMainView?: (feedId: number) => void;
  feedDetailFrames?: FeedDetailFrames;
  feedDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?: (
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ) => ReactNode;
}

export function FeedHealthDrawer({
  actions,
  busy,
  healthError,
  healthLoading,
  healthOpenId,
  healthFeed,
  openHealth,
  onOpenFeedHealthMainView,
  feedDetailFrames,
  feedDetailContributions = EMPTY_CONTRIBUTIONS,
  renderPluginDetailTab,
}: FeedHealthDrawerProps) {
  // Active tab is the SHORT NAME for first-party tabs (pinned testids/content
  // dispatch) or the contributionId for plugin tabs.
  const [tabState, setTabState] = useState<{ sourceId: number | null; tab: string }>({
    sourceId: null,
    tab: 'summary',
  });
  const Frames = feedDetailFrames ?? PLAIN_FEED_DETAIL_FRAMES;

  if (healthOpenId == null) return null;

  const detailTabs = feedDetailContributions.filter(
    (contribution) =>
      contribution.mode === 'tab' &&
      (contribution.status === 'enabled' || contribution.status === 'disabled'),
  );
  const activeTab = tabState.sourceId === healthOpenId ? tabState.tab : 'summary';
  const setActiveTab = (tab: string) => setTabState({ sourceId: healthOpenId, tab });
  const subject: PluginDetailSubject = { kind: 'source', sourceId: String(healthOpenId) };
  const activePluginTab = detailTabs.find(
    (contribution) =>
      contribution.runtimeSource === 'runtimeIndex' &&
      contribution.contributionId === activeTab,
  );

  return (
    <aside className="panel-card" data-testid="source-health-drawer">
      <div className="control-row">
        <div className="panel-item-main">
          <div className="panel-item-title">{healthFeed?.name ?? 'Feed details'}</div>
          {healthFeed && <div className="panel-item-meta">{feedKindLabel(healthFeed.kind)}</div>}
        </div>
        {onOpenFeedHealthMainView && (
          <Button
            type="button"

            data-testid={`source-health-open-mainView-${healthOpenId}`}
            onClick={() => {
              onOpenFeedHealthMainView(healthOpenId);
              actions.closeHealth();
            }}
          >
            Open main view
          </Button>
        )}
        <WindowCloseButton size="compact" label="Close feed details" onClick={actions.closeHealth} />
      </div>

      <div className="tab-list" role="tablist" aria-label="Feed detail">
        {detailTabs.map((tab) => {
          const firstParty = FIRST_PARTY_FEED_DETAIL_TABS[tab.contributionId];
          const tabKey = firstParty?.shortName ?? tab.contributionId;
          const testId = firstParty
            ? `source-detail-tab-${firstParty.shortName}`
            : `source-detail-tab-${contributionSlug(tab.contributionId)}`;
          const button = (
            <TabButton
              key={tab.placementId}
              type="button"
              role="tab"
              className="tab-button"
              data-testid={testId}
              data-contribution-id={tab.contributionId}
              data-runtime-source={tab.runtimeSource}
              aria-selected={activeTab === tabKey}
              onClick={() => setActiveTab(tabKey)}
            >
              {tab.shortTitle ?? tab.title}
            </TabButton>
          );
          if (!firstParty) return button;
          const Frame = Frames[firstParty.frameKey];
          return <Frame key={tab.placementId}>{button}</Frame>;
        })}
      </div>
      {activePluginTab && renderPluginDetailTab
        ? renderPluginDetailTab(activePluginTab, subject)
        : null}

      {healthError && (
        <PanelError testId="source-health-error">{healthError}</PanelError>
      )}
      {healthLoading && !openHealth ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : openHealth ? (
        <>
          {activeTab === 'summary' && (
            <div className="panel-card panel-item-meta" data-testid="source-detail-summary-panel">
              <strong className="panel-item-title">{openHealth.source.name}</strong>
              <span>{feedKindLabel(openHealth.source.kind)}</span>
              <span>{healthFeed ? feedConfigSummary(healthFeed) : redactedUrl(openHealth.source.url) ?? 'feed details'}</span>
              <span>
                {openHealth.source.enabled ? 'enabled' : 'disabled'} /{' '}
                <span data-testid={`source-health-status-${healthOpenId}`}>
                  {statusLabel(openHealth.summary.status)}
                </span>
              </span>
            </div>
          )}

          {activeTab === 'health' && <FeedHealthSummary health={openHealth} sourceId={healthOpenId} />}

          <div className="control-row panel-item-meta">
            <Button
              type="button"

              data-testid={`source-health-retry-${healthOpenId}`}
              disabled={busy === healthOpenId}
              onClick={() => { void actions.fetchNow(healthOpenId); }}
            >
              <RefreshCw size={12} className={busy === healthOpenId ? 'spin' : undefined} />
              Check now
            </Button>
            <span data-testid={`source-health-cost-${healthOpenId}`}>
              Cost {formatCost(openHealth.costs.recentActualMicro)}
            </span>
          </div>

          {activeTab === 'runs' && <FeedHealthChecks health={openHealth} sourceId={healthOpenId} />}
        </>
      ) : null}
    </aside>
  );
}
