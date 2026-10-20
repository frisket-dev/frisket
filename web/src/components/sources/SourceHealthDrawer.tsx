import { useState } from 'react';
import type { ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';
import type { SourceHealth, SourceInfo } from '../../api/open';
import type { WorkbenchResolvedLayoutContribution } from '../../workbench/layout';
import type { PluginDetailSubject } from '../../workbench/pluginDetailContext';
import { Button, TabButton } from '../Button';
import { PanelEmpty, PanelError } from '../PanelPrimitives';
import { WindowCloseButton } from '../WindowCloseButton';
import { redactedUrl, sourceConfigSummary, sourceKindLabel } from './sourceConfig';
import type { SourceDetailFrames } from './sourceFrames';
import { SourceHealthSummary, SourceHealthRuns } from './SourceHealthContent';
import { formatCost, statusLabel } from './sourceFormatting';
import type { BusySource } from './sourceState';
import type { SourcePanelActions } from './useSourcesPanelState';

const PlainSourceDetailFrame = ({ children }: { children: ReactNode }) => <>{children}</>;
const PLAIN_SOURCE_DETAIL_FRAMES: SourceDetailFrames = {
  Summary: PlainSourceDetailFrame,
  Health: PlainSourceDetailFrame,
  Runs: PlainSourceDetailFrame,
};

const EMPTY_CONTRIBUTIONS: WorkbenchResolvedLayoutContribution[] = [];

// First-party source detail tab bindings: legacy short names (pinned testids)
// + which contribution frame wraps each tab button. The tab SET renders from
// resolved sourceDetail contributions — no hardcoded tab union.
const FIRST_PARTY_SOURCE_DETAIL_TABS: Record<
  string,
  { shortName: 'summary' | 'health' | 'runs'; frameKey: keyof SourceDetailFrames }
> = {
  'frisket.core.view.source_summary': { shortName: 'summary', frameKey: 'Summary' },
  'frisket.core.view.source_health': { shortName: 'health', frameKey: 'Health' },
  'frisket.core.view.source_runs': { shortName: 'runs', frameKey: 'Runs' },
};
interface SourceHealthDrawerProps {
  actions: SourcePanelActions;
  busy: BusySource;
  healthError: string | null;
  healthLoading: boolean;
  healthOpenId: number | null;
  healthSource: SourceInfo | null;
  openHealth: SourceHealth | null;
  onOpenSourceHealthMainView?: (sourceId: number) => void;
  sourceDetailFrames?: SourceDetailFrames;
  sourceDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?: (
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ) => ReactNode;
}

export function SourceHealthDrawer({
  actions,
  busy,
  healthError,
  healthLoading,
  healthOpenId,
  healthSource,
  openHealth,
  onOpenSourceHealthMainView,
  sourceDetailFrames,
  sourceDetailContributions = EMPTY_CONTRIBUTIONS,
  renderPluginDetailTab,
}: SourceHealthDrawerProps) {
  // Active tab is the SHORT NAME for first-party tabs (pinned testids/content
  // dispatch) or the contributionId for plugin tabs.
  const [tabState, setTabState] = useState<{ sourceId: number | null; tab: string }>({
    sourceId: null,
    tab: 'summary',
  });
  const Frames = sourceDetailFrames ?? PLAIN_SOURCE_DETAIL_FRAMES;

  if (healthOpenId == null) return null;

  const detailTabs = sourceDetailContributions.filter(
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
          <div className="panel-item-title">{healthSource?.name ?? 'Source details'}</div>
          {healthSource && <div className="panel-item-meta">{sourceKindLabel(healthSource.kind)}</div>}
        </div>
        {onOpenSourceHealthMainView && (
          <Button
            type="button"

            data-testid={`source-health-open-mainView-${healthOpenId}`}
            onClick={() => {
              onOpenSourceHealthMainView(healthOpenId);
              actions.closeHealth();
            }}
          >
            Open main view
          </Button>
        )}
        <WindowCloseButton size="compact" label="Close source details" onClick={actions.closeHealth} />
      </div>

      <div className="tab-list" role="tablist" aria-label="Source detail">
        {detailTabs.map((tab) => {
          const firstParty = FIRST_PARTY_SOURCE_DETAIL_TABS[tab.contributionId];
          const tabKey = firstParty?.shortName ?? tab.contributionId;
          const testId = firstParty
            ? `source-detail-tab-${firstParty.shortName}`
            : `source-detail-tab-${tab.contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`;
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
              <span>{sourceKindLabel(openHealth.source.kind)}</span>
              <span>{healthSource ? sourceConfigSummary(healthSource) : redactedUrl(openHealth.source.url) ?? 'source details'}</span>
              <span>
                {openHealth.source.enabled ? 'enabled' : 'disabled'} /{' '}
                <span data-testid={`source-health-status-${healthOpenId}`}>
                  {statusLabel(openHealth.summary.status)}
                </span>
              </span>
            </div>
          )}

          {activeTab === 'health' && <SourceHealthSummary health={openHealth} sourceId={healthOpenId} />}

          <div className="control-row panel-item-meta">
            <Button
              type="button"

              data-testid={`source-health-retry-${healthOpenId}`}
              disabled={busy === healthOpenId}
              onClick={() => { void actions.fetchNow(healthOpenId); }}
            >
              <RefreshCw size={12} className={busy === healthOpenId ? 'spin' : undefined} />
              Poll now
            </Button>
            <span data-testid={`source-health-cost-${healthOpenId}`}>
              Cost {formatCost(openHealth.costs.recentActualMicro)}
            </span>
          </div>

          {activeTab === 'runs' && <SourceHealthRuns health={openHealth} sourceId={healthOpenId} />}
        </>
      ) : null}
    </aside>
  );
}
