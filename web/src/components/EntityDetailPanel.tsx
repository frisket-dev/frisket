import { useState, type ReactNode } from 'react';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import type { PluginDetailSubject } from '../workbench/pluginDetailContext';
import {
  EntityDetailConnectionsTabFrame,
  EntityDetailEvidenceTabFrame,
  EntityDetailSummaryTabFrame,
} from '../workbench/contributions';

// EntityDetailPanel — the entity-detail host, extracted from the retired FtM
// GraphNeighborhoodView so it survives the graph-view rework
// (graph-view-generic-ui-v1). It is subject-driven (any endpoint node, not an
// FtM anchor) and renders the resolved entityDetail contributions: first-party
// summary/evidence/connections panels + plugin detail tabs via
// renderPluginDetailTab. The connections panel now reads the SELECTED node's
// incident edges (generic), no longer the FtM anchor's neighborhood; the FtM
// /graph/neighborhood BACKEND service is retained but is an entityDetail-host
// concern kept as a separate follow-up.

// First-party entity-detail tab bindings: legacy short names (pinned testids)
// + the frame carrying each tab's resolved-placement metadata.
const FIRST_PARTY_ENTITY_DETAIL_TABS: Record<
  string,
  { shortName: string; Frame: (props: { children: ReactNode }) => ReactNode }
> = {
  'frisket.investigative.view.entity_summary': {
    shortName: 'summary',
    Frame: EntityDetailSummaryTabFrame,
  },
  'frisket.investigative.view.entity_evidence': {
    shortName: 'evidence',
    Frame: EntityDetailEvidenceTabFrame,
  },
  'frisket.investigative.view.entity_connections': {
    shortName: 'connections',
    Frame: EntityDetailConnectionsTabFrame,
  },
};

function entityDetailTabTestId(contribution: WorkbenchResolvedLayoutContribution): string {
  const firstParty = FIRST_PARTY_ENTITY_DETAIL_TABS[contribution.contributionId];
  if (firstParty) return `entity-detail-tab-${firstParty.shortName}`;
  return `entity-detail-tab-${contribution.contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`;
}

export interface EntityDetailConnection {
  key: string;
  label: string;
  detail?: string;
}

export function EntityDetailPanel({
  subject,
  summary,
  connections,
  contributions,
  renderPluginDetailTab,
}: {
  subject: PluginDetailSubject;
  summary: { label: string; meta: string };
  connections: EntityDetailConnection[];
  contributions: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?: (
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ) => ReactNode;
}) {
  const [activeContributionId, setActiveContributionId] = useState<string | null>(null);

  const tabs = contributions.filter(
    (contribution) =>
      contribution.mode === 'tab' &&
      (contribution.status === 'enabled' || contribution.status === 'disabled'),
  );
  if (tabs.length === 0) return null;
  const activeTab =
    tabs.find((tab) => tab.contributionId === activeContributionId) ?? tabs[0];

  const renderFirstPartyPanel = (contributionId: string) => {
    switch (contributionId) {
      case 'frisket.investigative.view.entity_summary':
        return (
          <div className="entity-detail-panel" data-testid="entity-detail-summary-panel">
            <strong>{summary.label}</strong>
            <span>{summary.meta}</span>
          </div>
        );
      case 'frisket.investigative.view.entity_evidence':
        return (
          <div className="entity-detail-panel" data-testid="entity-detail-evidence-panel">
            Evidence opens through contributed row and cell evidence surfaces when available.
          </div>
        );
      case 'frisket.investigative.view.entity_connections':
        return (
          <div className="entity-detail-panel" data-testid="entity-detail-connections-panel">
            {connections.length === 0 ? (
              <span>No connections for this node.</span>
            ) : (
              connections.map((connection) => (
                <span key={connection.key}>
                  {connection.label}
                  {connection.detail ? ` · ${connection.detail}` : ''}
                </span>
              ))
            )}
          </div>
        );
      default:
        return null;
    }
  };

  return (
    <section className="entity-detail-shell" data-testid="entity-detail-shell">
      <div className="entity-detail-tabstrip" role="tablist" aria-label="Entity detail">
        {tabs.map((tab) => {
          const firstParty = FIRST_PARTY_ENTITY_DETAIL_TABS[tab.contributionId];
          const button = (
            <button
              key={tab.placementId}
              type="button"
              role="tab"
              className="entity-detail-tab"
              data-testid={entityDetailTabTestId(tab)}
              data-contribution-id={tab.contributionId}
              data-runtime-source={tab.runtimeSource}
              aria-selected={activeTab.contributionId === tab.contributionId}
              onClick={() => setActiveContributionId(tab.contributionId)}
            >
              {tab.shortTitle ?? tab.title}
            </button>
          );
          if (!firstParty) return button;
          const Frame = firstParty.Frame;
          return <Frame key={tab.placementId}>{button}</Frame>;
        })}
      </div>

      {FIRST_PARTY_ENTITY_DETAIL_TABS[activeTab.contributionId]
        ? renderFirstPartyPanel(activeTab.contributionId)
        : renderPluginDetailTab?.(activeTab, subject) ?? null}
    </section>
  );
}
