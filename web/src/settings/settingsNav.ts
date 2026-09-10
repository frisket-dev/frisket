import {
  SETTINGS_NAV_GROUPS,
  SETTINGS_SECTIONS,
  settingsRoutePatternForSection,
  type SettingsNavGroup,
  type SettingsSectionDefinition,
} from './settingsRegistry';

export interface SettingsNavContext {
  projectId?: string;
  identityMode: boolean;
  query?: string;
}

export interface SettingsNavItem {
  definition: SettingsSectionDefinition;
  disabled: boolean;
  disabledReason: string | null;
  path: string;
}

const normalizedQuery = (query: string | undefined): string => (
  query?.trim().toLowerCase() ?? ''
);

function matchesQuery(definition: SettingsSectionDefinition, query: string): boolean {
  if (!query) return true;
  return [
    definition.title,
    definition.navGroup,
    definition.scope,
    definition.section,
    ...definition.searchLabels,
  ].some((label) => label.toLowerCase().includes(query));
}

export function settingsItemAvailability(
  definition: SettingsSectionDefinition,
  context: SettingsNavContext,
): Pick<SettingsNavItem, 'disabled' | 'disabledReason' | 'path'> {
  if (definition.visibility === 'project' && !context.projectId) {
    return {
      disabled: true,
      disabledReason: 'Open a project to manage project settings.',
      path: settingsRoutePatternForSection(definition),
    };
  }
  if (definition.visibility === 'hosted-only' && !context.identityMode) {
    return {
      disabled: true,
      disabledReason: 'Hosted organization settings are unavailable in local mode.',
      path: settingsRoutePatternForSection(definition),
    };
  }
  if (definition.visibility === 'local-only' && context.identityMode) {
    return {
      disabled: true,
      disabledReason: 'Managed by your organization in hosted mode.',
      path: settingsRoutePatternForSection(definition),
    };
  }
  return {
    disabled: false,
    disabledReason: null,
    path: settingsRoutePatternForSection(definition, context.projectId),
  };
}

function settingsNavItems(
  context: SettingsNavContext,
  definitions: readonly SettingsSectionDefinition[] = SETTINGS_SECTIONS,
): SettingsNavItem[] {
  const query = normalizedQuery(context.query);
  return definitions.flatMap((definition) => {
    if (definition.visibility === 'local-only' && context.identityMode) return [];
    if (!matchesQuery(definition, query)) return [];
    if (definition.visibility === 'project' && !context.projectId) return [];
    return [{ definition, ...settingsItemAvailability(definition, context) }];
  });
}

export function groupedSettingsNavItems(
  context: SettingsNavContext,
  definitions?: readonly SettingsSectionDefinition[],
): Array<{ group: SettingsNavGroup; items: SettingsNavItem[] }> {
  const items = settingsNavItems(context, definitions);
  return SETTINGS_NAV_GROUPS.flatMap((group) => {
    const groupItems = items.filter((item) => item.definition.navGroup === group);
    return groupItems.length > 0 ? [{ group, items: groupItems }] : [];
  });
}
