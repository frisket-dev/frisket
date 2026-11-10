import { useEffect, useRef } from 'react';

import { useTelemetryDisclosureReady } from './disclosureReady';
import { sendProductTelemetry, type SettingsTelemetrySection, type WorkspaceTelemetryPanel, type WorkspaceTelemetryView } from './productTelemetry';

export type { SettingsTelemetrySection, WorkspaceTelemetryPanel, WorkspaceTelemetryView } from './productTelemetry';

const PANEL_KINDS: Readonly<Record<string, WorkspaceTelemetryPanel>> = {
  'frisket.investigative.panel.friendly_filters': 'filter',
  'frisket.investigative.panel.mentions': 'mentions',
  'frisket.core.panel.sources': 'sources',
  'frisket.core.panel.saved_views': 'views',
  'frisket.core.panel.watches': 'watches',
  'frisket.core.panel.notifications': 'notifications',
  'frisket.embeddings.panel.indexes': 'embeddings',
  'frisket.core.panel.jobs': 'jobs',
  'frisket.core.panel.errors': 'errors',
  'frisket.core.panel.history': 'history',
  'frisket.core.panel.lineage': 'lineage',
};

/** Extension identities stay local; only this fixed feature vocabulary leaves. */
export function telemetryPanelKind(contributionId: string): WorkspaceTelemetryPanel {
  return Object.hasOwn(PANEL_KINDS, contributionId) ? PANEL_KINDS[contributionId] : 'plugin';
}

export function useWorkspaceNavigationTelemetry({ projectId, view, rightPanels, bottomPanel }: {
  projectId: string | null | undefined;
  view?: WorkspaceTelemetryView | null;
  rightPanels?: readonly WorkspaceTelemetryPanel[];
  bottomPanel?: WorkspaceTelemetryPanel | null;
}) {
  const ready = useTelemetryDisclosureReady();
  const rightKey = [...new Set(rightPanels)].sort().join('|');
  const last = useRef<{ projectId: string; view: WorkspaceTelemetryView | null; rightKey: string; bottomPanel: WorkspaceTelemetryPanel | null } | null>(null);
  useEffect(() => {
    if (!ready || !projectId) { last.current = null; return; }
    const previous = last.current?.projectId === projectId ? last.current : null;
    if (view && view !== previous?.view) {
      sendProductTelemetry({ type: 'Workspace.viewOpened', properties: { view } }, projectId);
    }
    const previousPanels = new Set(previous?.rightKey.split('|') ?? []);
    for (const panel of rightKey.split('|').filter(Boolean) as WorkspaceTelemetryPanel[]) {
      if (!previousPanels.has(panel)) sendProductTelemetry({ type: 'Workspace.panelOpened', properties: { region: 'right', panel } }, projectId);
    }
    if (bottomPanel && bottomPanel !== previous?.bottomPanel) {
      sendProductTelemetry({ type: 'Workspace.panelOpened', properties: { region: 'bottom', panel: bottomPanel } }, projectId);
    }
    last.current = { projectId, view: view ?? null, rightKey, bottomPanel: bottomPanel ?? null };
  }, [ready, projectId, view, rightKey, bottomPanel]);
}

const SETTINGS_SECTIONS: Readonly<Record<string, SettingsTelemetrySection>> = {
  'personal.profile': 'profile',
  'personal.preferences': 'preferences',
  'personal.privacy': 'privacy',
  'personal.ai-providers': 'ai_providers',
  'personal.diagnostics': 'diagnostics',
  'personal.skills': 'skills',
  'organization.ai-providers': 'ai_providers',
  'organization.env-vars': 'env_vars',
  'organization.api-keys': 'api_keys',
  'organization.spend': 'spend',
  'organization.skills': 'skills',
  'organization.modelsGateway': 'models_gateway',
  'project.general': 'general',
  'project.access': 'access',
  'project.ai-providers': 'ai_providers',
  'project.secrets': 'secrets',
  'project.mcp-servers': 'mcp_servers',
  'project.notifications': 'notifications',
  'project.plugins': 'plugins',
  'project.data-management': 'data_management',
};

export function useSettingsNavigationTelemetry({ scope, sectionId, projectId, available }: {
  scope: 'personal' | 'organization' | 'project';
  sectionId: string | null;
  projectId?: string;
  available: boolean;
}) {
  const ready = useTelemetryDisclosureReady();
  const last = useRef<string | null>(null);
  useEffect(() => {
    if (!ready || !available || sectionId == null || (scope === 'project' && !projectId)) {
      last.current = null;
      return;
    }
    const key = JSON.stringify([scope, sectionId, scope === 'project' ? projectId : null]);
    if (last.current === key) return;
    last.current = key;
    const section = Object.hasOwn(SETTINGS_SECTIONS, sectionId) ? SETTINGS_SECTIONS[sectionId] : 'extension';
    if (scope === 'project') sendProductTelemetry({ type: 'Project.settingsSectionOpened', properties: { section } }, projectId);
    else sendProductTelemetry({ type: 'Settings.sectionOpened', properties: { scope, section } });
  }, [ready, available, scope, sectionId, projectId]);
}
