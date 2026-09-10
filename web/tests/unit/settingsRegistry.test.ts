// Pure registry-contract assertions over SETTINGS_SECTIONS plus
// routePath/settingsRoutePatternForSection need no page or DOM. Live app-shell
// tests separately cover registry rendering and invalid-section rejection.

import { describe, expect, it } from 'vitest';
import { routePath, type SettingsRoute } from '../../src/routes';
import {
  SETTINGS_SECTIONS,
  settingsRoutePatternForSection,
} from '../../src/settings/settingsRegistry';
import { settingsSectionsFor } from '../../src/settings/openSettingsRegistry';
import {
  immutableEditionDescriptor,
  LOCAL_EDITION_DESCRIPTOR,
  TEAM_EDITION_DESCRIPTOR,
} from '../../src/editions/posture';

const SAMPLE_PROJECT_ID = 'alpha-project';
const ALLOWED_SCOPES = new Set(['personal', 'project', 'organization']);
const ALLOWED_GROUPS = new Set(['Personal', 'Project', 'Organization']);
const ALLOWED_VISIBILITY = new Set(['always', 'project', 'hosted-only', 'local-only']);
const ALLOWED_PERMISSION = new Set([
  'personal',
  'viewer',
  'editor',
  'owner',
  'organization-admin',
]);
const FORBIDDEN_SETTING_SECTIONS = new Set([
  'source',
  'sources',
  'schedule',
  'schedules',
  'action',
  'actions',
  'run-option',
  'run-options',
]);

describe('settings registry contract', () => {
  it('has unique ids and only admitted V1 sections', () => {
    const ids = SETTINGS_SECTIONS.map((definition) => definition.id);
    expect(new Set(ids).size).toBe(ids.length);

    for (const definition of SETTINGS_SECTIONS) {
      expect(definition.id).toBe(`${definition.scope}.${definition.section}`);
      expect(ALLOWED_SCOPES.has(definition.scope)).toBeTruthy();
      expect(ALLOWED_GROUPS.has(definition.navGroup)).toBeTruthy();
      expect(ALLOWED_VISIBILITY.has(definition.visibility)).toBeTruthy();
      expect(ALLOWED_PERMISSION.has(definition.permission)).toBeTruthy();
      expect(definition.routePattern).toContain(`/settings/${definition.scope}/`);
      expect(definition.title.trim().length).toBeGreaterThan(0);
      expect(definition.searchLabels.length).toBeGreaterThan(0);
      expect(definition.component.trim().length).toBeGreaterThan(0);
      expect(FORBIDDEN_SETTING_SECTIONS.has(definition.section)).toBeFalsy();
    }

    expect(ids).toContain('personal.profile');
    expect(ids).toContain('personal.ai-providers');
    expect(ids).toContain('project.general');
    expect(ids).toContain('organization.ai-providers');
    expect(ids).not.toContain('project.sources');
    expect(ids).not.toContain('project.schedules');
    expect(ids).not.toContain('project.actions');
    expect(ids).not.toContain('project.run-options');
  });

  it('route patterns match routePath output', () => {
    for (const definition of SETTINGS_SECTIONS) {
      const route: SettingsRoute = {
        kind: 'settings',
        scope: definition.scope,
        section: definition.section,
        projectId: definition.scope === 'project' ? SAMPLE_PROJECT_ID : undefined,
      };
      expect(routePath(route)).toBe(settingsRoutePatternForSection(definition, SAMPLE_PROJECT_ID));
    }
  });

  it('keeps base settings resolvable across open edition postures without duplicates', () => {
    const local = settingsSectionsFor(LOCAL_EDITION_DESCRIPTOR)
      .map((definition) => definition.id);
    const team = settingsSectionsFor(TEAM_EDITION_DESCRIPTOR)
      .map((definition) => definition.id);

    for (const ids of [local, team]) {
      expect(new Set(ids).size).toBe(ids.length);
    }
    expect(local).toContain('personal.profile');
    expect(local).toContain('project.notifications');
    expect(local).toContain('organization.ai-providers');
    expect(local).toContain('organization.api-keys');
    expect(local).not.toContain('project.access');
    expect(team).toContain('project.access');
    expect(team).toContain('project.notifications');
  });

  it('removes notification destination settings when the edition does not configure them', () => {
    const editionId = 'managed-notification-destinations';
    const edition = immutableEditionDescriptor({
      id: editionId,
      capabilities: {
        configurableNotificationDestinations: false,
        configurableNotificationEmail: false,
        identity: true,
        team: true,
      },
    });

    const ids = settingsSectionsFor(edition).map((definition) => definition.id);
    expect(ids).not.toContain('project.notifications');
    expect(ids).toContain('project.general');
    expect(ids).toContain('project.access');
  });

  it('accepts an edition-owned component only with its renderer', () => {
    const sections = settingsSectionsFor(TEAM_EDITION_DESCRIPTOR, [{
        id: 'private.billing',
        scope: 'organization',
        section: 'private-billing',
        routePattern: '/settings/organization/private-billing',
        title: 'Private billing',
        navGroup: 'Organization',
        searchLabels: ['private billing'],
        visibility: 'hosted-only',
        permission: 'organization-admin',
        component: 'private.billing',
        summary: 'Composition-owned section.',
        handler: () => null,
      }]);

    const section = sections.find((candidate) => candidate.id === 'private.billing');
    expect(section).toBeDefined();
    expect(section && 'handler' in section).toBe(true);
  });

  it('fails closed if an untyped contribution omits its renderer', () => {
    expect(() => settingsSectionsFor(TEAM_EDITION_DESCRIPTOR, [{
        id: 'private.unrendered',
        scope: 'organization',
        section: 'private-unrendered',
        routePattern: '/settings/organization/private-unrendered',
        title: 'Private unrendered',
        navGroup: 'Organization',
        searchLabels: ['private'],
        visibility: 'hosted-only',
        permission: 'organization-admin',
        component: 'private.unrendered',
        summary: 'Must not reach a public fallback.',
      }] as never)).toThrow('missing handler');
  });

  it('rejects a contributed section whose id duplicates any active section', () => {
    expect(() => settingsSectionsFor(TEAM_EDITION_DESCRIPTOR, [{
        id: 'personal.profile',
        scope: 'organization',
        section: 'profile-alias',
        routePattern: '/settings/organization/profile-alias',
        title: 'Profile alias',
        navGroup: 'Organization',
        searchLabels: ['profile alias'],
        visibility: 'hosted-only',
        permission: 'organization-admin',
        component: 'extension.profile-alias',
        summary: 'Must not shadow a base id.',
        handler: () => null,
      }])).toThrow('Duplicate settings section id: personal.profile');
  });

  it('rejects two ids that claim the same scope and section location', () => {
    expect(() => settingsSectionsFor(TEAM_EDITION_DESCRIPTOR, [{
        id: 'extension.profile-shadow',
        scope: 'personal',
        section: 'profile',
        routePattern: '/settings/personal/profile',
        title: 'Profile shadow',
        navGroup: 'Personal',
        searchLabels: ['profile shadow'],
        visibility: 'always',
        permission: 'personal',
        component: 'extension.profile-shadow',
        summary: 'Must not shadow a base location.',
        handler: () => null,
      }])).toThrow('Duplicate settings section location: personal/profile');
  });
});
