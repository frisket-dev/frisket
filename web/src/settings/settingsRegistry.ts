export type SettingsScope = 'personal' | 'project' | 'organization';

export type SettingsNavGroup = 'Personal' | 'Project' | 'Organization';

// 'local-only' is hosted-only's mirror: enabled in the local tier, disabled
// under a hosted identity (workspace provider keys are org-managed in
// hosted mode).
export type SettingsVisibility = 'always' | 'project' | 'hosted-only' | 'local-only';

export type SettingsPermission =
  | 'personal'
  | 'viewer'
  | 'editor'
  | 'owner'
  | 'organization-admin';

export type SettingsSectionComponentKey =
  | 'personal.profile'
  | 'personal.preferences'
  | 'personal.privacy'
  | 'personal.aiProviders'
  | 'personal.diagnostics'
  | 'organization.aiProviders'
  | 'organization.secrets'
  | 'organization.apiKeys'
  | 'organization.spend'
  | 'project.general'
  | 'project.access'
  | 'project.aiProviders'
  | 'project.secrets'
  | 'project.mcpServers'
  | 'project.notifications'
  | 'project.plugins'
  | 'project.dataManagement';

export interface SettingsSectionDefinition {
  readonly id: string;
  readonly scope: SettingsScope;
  readonly section: string;
  readonly routePattern: string;
  readonly title: string;
  readonly navGroup: SettingsNavGroup;
  readonly searchLabels: readonly string[];
  readonly visibility: SettingsVisibility;
  readonly permission: SettingsPermission;
  readonly component: string;
  readonly summary: string;
}

export type OpenSettingsSectionDefinition = Omit<
  SettingsSectionDefinition,
  'component'
> & {
  readonly component: SettingsSectionComponentKey;
};

export interface SettingDefinition<TValue = unknown> {
  id: string;
  scope: SettingsScope;
  schema: Record<string, unknown>;
  defaultValue: TValue;
  sourceOrder: string[];
  storageOwner: 'personal' | 'project-local' | 'project' | 'app';
  permission: SettingsPermission;
  renderer: string;
}

export const SETTINGS_DEFAULT_SECTIONS: Record<SettingsScope, string> = {
  personal: 'profile',
  project: 'general',
  organization: 'ai-providers',
};

const projectRoute = (section: string) => `/p/{projectId}/settings/project/${section}`;
const globalRoute = (scope: Exclude<SettingsScope, 'project'>, section: string) => (
  `/settings/${scope}/${section}`
);

export const SETTINGS_SECTIONS: readonly OpenSettingsSectionDefinition[] = [
  {
    id: 'personal.profile',
    scope: 'personal',
    section: 'profile',
    routePattern: globalRoute('personal', 'profile'),
    title: 'Profile',
    navGroup: 'Personal',
    searchLabels: ['profile', 'identity', 'email', 'display name'],
    visibility: 'always',
    permission: 'personal',
    component: 'personal.profile',
    summary: 'Identity and sign-in.',
  },
  {
    id: 'personal.preferences',
    scope: 'personal',
    section: 'preferences',
    routePattern: globalRoute('personal', 'preferences'),
    title: 'Preferences',
    navGroup: 'Personal',
    searchLabels: ['preferences', 'theme', 'appearance'],
    visibility: 'always',
    permission: 'personal',
    component: 'personal.preferences',
    summary: 'Theme and appearance.',
  },
  {
    id: 'personal.privacy',
    scope: 'personal',
    section: 'privacy',
    routePattern: globalRoute('personal', 'privacy'),
    title: 'Privacy',
    navGroup: 'Personal',
    searchLabels: ['privacy', 'product telemetry', 'analytics', 'opt out'],
    visibility: 'always',
    permission: 'personal',
    component: 'personal.privacy',
    summary: 'Product telemetry preference.',
  },
  {
    // The workspace-level provider config (keys in
    // <ws>/.frisket/provider_keys.json + the Ollama URL) — the surface the
    // ModelPicker's inline pane used to hold.
    // Hosted identities manage keys at organization.ai-providers instead.
    id: 'personal.ai-providers',
    scope: 'personal',
    section: 'ai-providers',
    routePattern: globalRoute('personal', 'ai-providers'),
    title: 'AI Providers',
    navGroup: 'Personal',
    searchLabels: ['ai providers', 'provider keys', 'models', 'ollama', 'lm studio', 'local server', 'api keys'],
    visibility: 'local-only',
    permission: 'personal',
    component: 'personal.aiProviders',
    summary: 'Provider keys and your local AI server for this workspace.',
  },
  {
    // The self-probe canonical home: the same GET /api/diagnose facts
    // DiagnosePanel's <dialog> used to render, now a Settings section. Placed
    // adjacent to personal.ai-providers: the probes it reports (model
    // providers, ollama, local engines, models sidecar, media toolbelt) are
    // instance/workspace-level, the same tier as that section.
    // `visibility: 'always'` (not 'local-only' like personal.ai-providers)
    // because the self-probe is not a provider-key management surface
    // reassigned to organization.ai-providers under a hosted identity — it
    // stays the one diagnostic surface in every posture, matching the modal
    // it replaces (never gated on identityMode).
    id: 'personal.diagnostics',
    scope: 'personal',
    section: 'diagnostics',
    routePattern: globalRoute('personal', 'diagnostics'),
    title: 'Diagnostics',
    navGroup: 'Personal',
    searchLabels: ['diagnostics', 'diagnose', 'self-probe', 'health', 'engines', 'sidecar', 'media toolbelt'],
    visibility: 'always',
    permission: 'personal',
    component: 'personal.diagnostics',
    summary: 'Self-probe: model providers, local engines, and sidecars.',
  },
  {
    id: 'project.general',
    scope: 'project',
    section: 'general',
    routePattern: projectRoute('general'),
    title: 'General',
    navGroup: 'Project',
    searchLabels: ['project', 'general', 'name', 'description'],
    visibility: 'project',
    permission: 'editor',
    component: 'project.general',
    summary: 'Project identity and lifecycle.',
  },
  // project.access is intentionally not listed here: it is a team-and-above
  // capability, not an unconditionally open project section. Its complete
  // definition lives as an explicit, typed capability-gated carve-out in
  // openSettingsRegistry.ts (CAPABILITY_GATED_OPEN_SECTIONS) so availability
  // is derived from edition posture rather than a second hand-copied id list.
  {
    id: 'project.ai-providers',
    scope: 'project',
    section: 'ai-providers',
    routePattern: projectRoute('ai-providers'),
    title: 'AI Providers',
    navGroup: 'Project',
    searchLabels: ['project', 'ai providers', 'provider keys', 'models'],
    visibility: 'project',
    permission: 'owner',
    component: 'project.aiProviders',
    summary: 'Project-scoped provider overrides.',
  },
  {
    id: 'project.secrets',
    scope: 'project',
    section: 'secrets',
    routePattern: projectRoute('secrets'),
    title: 'Secrets',
    navGroup: 'Project',
    searchLabels: ['project', 'secrets', 'environment variables', 'credentials'],
    visibility: 'project',
    permission: 'owner',
    component: 'project.secrets',
    summary: 'Project secret metadata.',
  },
  {
    id: 'project.mcp-servers', scope: 'project', section: 'mcp-servers',
    routePattern: projectRoute('mcp-servers'), title: 'MCP Servers', navGroup: 'Project',
    searchLabels: ['mcp', 'tools', 'stdio', 'local server'], visibility: 'local-only',
    permission: 'owner', component: 'project.mcpServers',
    summary: 'Trusted local MCP servers for Tool-assisted Extract.',
  },
  {
    id: 'project.plugins',
    scope: 'project',
    section: 'plugins',
    routePattern: projectRoute('plugins'),
    title: 'Plugins',
    navGroup: 'Project',
    searchLabels: ['project', 'plugins', 'trust', 'lifecycle'],
    visibility: 'project',
    permission: 'owner',
    component: 'project.plugins',
    summary: 'Workspace plugins, enabled per project.',
  },
  {
    id: 'project.data-management',
    scope: 'project',
    section: 'data-management',
    routePattern: projectRoute('data-management'),
    title: 'Data Management',
    navGroup: 'Project',
    searchLabels: [
      'project',
      'data management',
      'retention',
      'export',
      'delete',
      'media downloads',
      'private hosts',
      'network',
      'egress',
      'external services',
    ],
    visibility: 'project',
    permission: 'owner',
    component: 'project.dataManagement',
    summary: 'Retention, export, network, and media download policy.',
  },
  {
    id: 'organization.ai-providers',
    scope: 'organization',
    section: 'ai-providers',
    routePattern: globalRoute('organization', 'ai-providers'),
    title: 'AI Providers',
    navGroup: 'Organization',
    searchLabels: ['organization', 'ai providers', 'provider keys', 'models'],
    visibility: 'hosted-only',
    permission: 'organization-admin',
    component: 'organization.aiProviders',
    summary: 'Organization provider keys.',
  },
  {
    id: 'organization.env-vars',
    scope: 'organization',
    section: 'env-vars',
    routePattern: globalRoute('organization', 'env-vars'),
    title: 'Secrets',
    navGroup: 'Organization',
    searchLabels: ['organization', 'secrets', 'environment variables'],
    visibility: 'hosted-only',
    permission: 'organization-admin',
    component: 'organization.secrets',
    summary: 'Organization secret metadata.',
  },
  {
    id: 'organization.api-keys',
    scope: 'organization',
    section: 'api-keys',
    routePattern: globalRoute('organization', 'api-keys'),
    title: 'Frisket API Keys',
    navGroup: 'Organization',
    searchLabels: ['organization', 'api keys', 'tokens', 'pat'],
    visibility: 'hosted-only',
    permission: 'organization-admin',
    component: 'organization.apiKeys',
    summary: 'Frisket API tokens.',
  },
  {
    id: 'organization.spend',
    scope: 'organization',
    section: 'spend',
    routePattern: globalRoute('organization', 'spend'),
    title: 'Usage and Spend',
    navGroup: 'Organization',
    searchLabels: ['organization', 'usage', 'spend', 'models'],
    visibility: 'hosted-only',
    permission: 'organization-admin',
    component: 'organization.spend',
    summary: 'Provider usage and estimated spend.',
  },
];

export const SETTINGS_NAV_GROUPS: readonly SettingsNavGroup[] = [
  'Personal',
  'Project',
  'Organization',
];

export function settingsSectionId(scope: SettingsScope, section: string): string {
  return `${scope}.${section}`;
}

export function settingsRoutePatternForSection(
  definition: SettingsSectionDefinition,
  projectId = '{projectId}',
): string {
  return definition.scope === 'project'
    ? definition.routePattern.replace('{projectId}', encodeURIComponent(projectId))
    : definition.routePattern;
}
