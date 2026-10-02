import type { EditionDescriptor, PostureCapabilities } from '../editions/posture';
import {
  SETTINGS_SECTIONS,
  type OpenSettingsSectionDefinition,
  type SettingsSectionDefinition,
} from './settingsRegistry';
import type { ReactNode } from 'react';
import type { ProjectInfo } from '../api/types';

export interface EditionSettingsRenderProps {
  readonly project?: ProjectInfo | null;
  readonly identityMode: boolean;
}

export type EditionSettingsSection = SettingsSectionDefinition & {
  readonly handler: (props: EditionSettingsRenderProps) => ReactNode;
};

export type ActiveSettingsSection = OpenSettingsSectionDefinition | EditionSettingsSection;

/**
 * Open settings sections whose availability depends on an edition capability
 * rather than being unconditionally open. Each entry owns its complete
 * definition (never a bare id) so it never depends on being present in, or
 * absent from, SETTINGS_SECTIONS — the same storage-independence the private
 * per-edition contributions already have. This is the one explicit,
 * hand-written list the derivation needs; everything else in the open result
 * comes straight from SETTINGS_SECTIONS.
 */
type CapabilityGatedSection = OpenSettingsSectionDefinition & {
  readonly requiresCapability: keyof PostureCapabilities;
};

const CAPABILITY_GATED_OPEN_SECTIONS: readonly CapabilityGatedSection[] = [
  {
    id: 'personal.skills',
    scope: 'personal',
    section: 'skills',
    routePattern: '/settings/personal/skills',
    title: 'Skills',
    navGroup: 'Personal',
    searchLabels: ['skills', 'ask', 'instructions', 'skill.md'],
    visibility: 'local-only',
    permission: 'personal',
    component: 'personal.skills',
    summary: 'Instruction-only skills available to Ask.',
    requiresCapability: 'skills',
  },
  {
    id: 'organization.skills',
    scope: 'organization',
    section: 'skills',
    routePattern: '/settings/organization/skills',
    title: 'Skills',
    navGroup: 'Organization',
    searchLabels: ['skills', 'ask', 'instructions', 'skill.md'],
    visibility: 'hosted-only',
    permission: 'organization-admin',
    component: 'organization.skills',
    summary: 'Admitted instruction-only skills available to Ask.',
    requiresCapability: 'skills',
  },
  {
    id: 'project.access',
    scope: 'project',
    section: 'access',
    routePattern: '/p/{projectId}/settings/project/access',
    title: 'Access',
    navGroup: 'Project',
    searchLabels: ['project', 'access', 'members', 'invites'],
    visibility: 'project',
    permission: 'owner',
    component: 'project.access',
    summary: 'Members and invites.',
    requiresCapability: 'team',
  },
  {
    id: 'project.notifications',
    scope: 'project',
    section: 'notifications',
    routePattern: '/p/{projectId}/settings/project/notifications',
    title: 'Notifications',
    navGroup: 'Project',
    searchLabels: ['project', 'notifications', 'channels', 'routes', 'digests'],
    visibility: 'project',
    permission: 'owner',
    component: 'project.notifications',
    summary: 'Notification channels, routes, and delivery health.',
    requiresCapability: 'configurableNotificationDestinations',
  },
];

export function settingsSectionsFor(
  edition: Readonly<EditionDescriptor>,
  contributedSections: readonly EditionSettingsSection[] = [],
): ActiveSettingsSection[] {
  const capabilities = edition.capabilities;
  const gatedSections = CAPABILITY_GATED_OPEN_SECTIONS.filter(
    (section) => capabilities[section.requiresCapability],
  );
  for (const section of contributedSections) {
    if (typeof section.handler !== 'function') {
      throw new Error(`Contributed settings section is missing handler: ${section.id}`);
    }
  }
  const sections: ActiveSettingsSection[] = [
    ...SETTINGS_SECTIONS,
    ...gatedSections,
    ...contributedSections,
  ];
  const ids = new Set<string>();
  const locations = new Set<string>();
  for (const section of sections) {
    if (ids.has(section.id)) {
      throw new Error(`Duplicate settings section id: ${section.id}`);
    }
    ids.add(section.id);
    const location = `${section.scope}/${section.section}`;
    if (locations.has(location)) {
      throw new Error(`Duplicate settings section location: ${location}`);
    }
    locations.add(location);
  }
  return sections;
}
