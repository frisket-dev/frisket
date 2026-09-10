// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AdminOverview } from '../../src/api/types';
import {
  defineEditionModule,
  EditionModuleProvider,
  useEditionModule,
  type AdminOverviewSlotProps,
} from '../../src/editions/module';
import type { EditionRoute } from '../../src/routes/openRoutes';
import type { EditionSettingsSection } from '../../src/settings/openSettingsRegistry';
import type { WalkthroughDefinition } from '../../src/walkthrough/walkthroughs';
import { TEAM_EDITION_MODULE } from '../../src/editions/openModules';

afterEach(cleanup);

const descriptor = {
  id: 'synthetic',
  capabilities: {
    configurableNotificationDestinations: true,
    configurableNotificationEmail: true,
    identity: true,
    team: true,
  },
} as const;

describe('immutable edition module', () => {
  it('mounts Team with identity and team runtime capabilities', () => {
    expect(TEAM_EDITION_MODULE.descriptor.id).toBe('team');
    expect(TEAM_EDITION_MODULE.descriptor.capabilities.identity).toBe(true);
    expect(TEAM_EDITION_MODULE.descriptor.capabilities.team).toBe(true);
    expect(TEAM_EDITION_MODULE.routes.map((route) => route.id)).toContain('sign-in');
  });

  it('provides the admin slot supplied at mount', () => {
    const AdminOverviewSlot = vi.fn(
      ({ overview }: AdminOverviewSlotProps) => (
        <div data-testid="overview">projects:{overview.totals.projects ?? 0}</div>
      ),
    );
    const edition = defineEditionModule({ descriptor, AdminOverviewSlot });

    function Probe() {
      const { AdminOverviewSlot: Slot } = useEditionModule();
      const overview: Readonly<AdminOverview> = { totals: { projects: 7 } };
      return Slot ? <Slot overview={overview} /> : null;
    }

    render(
      <EditionModuleProvider edition={edition}>
        <Probe />
      </EditionModuleProvider>,
    );

    expect(screen.getByTestId('overview')).toHaveTextContent('projects:7');
    expect(AdminOverviewSlot).toHaveBeenCalledOnce();
    expect(Object.isFrozen(edition)).toBe(true);
    expect(Object.isFrozen(edition.routes)).toBe(true);
  });

  it('resolves walkthroughs once into the immutable module', () => {
    const guide: WalkthroughDefinition = {
      id: 'extension-guide',
      title: 'Extension guide',
      description: 'Composition-owned guide.',
      steps: [{
        id: 'open-sheet',
        title: 'Open sheet',
        instruction: 'Open the sheet.',
        target: { kind: 'sheet', id: 'Dispatches' },
        advance: 'click-target',
      }],
    };

    const edition = defineEditionModule({ descriptor, walkthroughs: [guide] });
    expect(edition.walkthroughs.map((walkthrough) => walkthrough.id)).toContain(
      'extension-guide',
    );
  });

  it('defensively copies and freezes every public contribution value', () => {
    const route = {
      id: 'synthetic-public',
      path: '/before',
      access: 'public',
      handler: () => null,
    } satisfies EditionRoute;
    const settingsSection = {
      id: 'organization.synthetic',
      scope: 'organization',
      section: 'synthetic',
      routePattern: '/settings/organization/synthetic',
      title: 'Before',
      navGroup: 'Organization',
      searchLabels: ['before'],
      visibility: 'hosted-only',
      permission: 'owner',
      component: 'organization.synthetic',
      summary: 'Synthetic settings.',
      handler: () => null,
    } satisfies EditionSettingsSection;
    const walkthrough = {
      id: 'synthetic-guide',
      title: 'Before',
      description: 'Synthetic walkthrough.',
      editions: ['synthetic'],
      steps: [{
        id: 'completed-run',
        title: 'Before',
        instruction: {
          lead: 'Before',
          code: 'before()',
          learnMore: { label: 'Before', href: '/before' },
        },
        target: {
          kind: 'completed-run',
          target: { kind: 'sheet', id: 'Before' },
        },
        advance: 'next-button',
      }],
    } satisfies WalkthroughDefinition;

    const edition = defineEditionModule({
      descriptor,
      routes: [route],
      settingsSections: [settingsSection],
      walkthroughs: [walkthrough],
    });
    const resolvedRoute = edition.routes.at(-1)!;
    const resolvedSection = edition.settingsSections.at(-1)!;
    const resolvedWalkthrough = edition.walkthroughs.find(
      (candidate) => candidate.id === walkthrough.id,
    )!;
    const resolvedStep = resolvedWalkthrough.steps[0];
    const resolvedInstruction = typeof resolvedStep.instruction === 'string'
      ? null
      : resolvedStep.instruction;
    const resolvedTarget = resolvedStep.target.kind === 'completed-run'
      ? resolvedStep.target.target
      : null;

    expect(Object.isFrozen(resolvedRoute)).toBe(true);
    expect(Object.isFrozen(resolvedSection)).toBe(true);
    expect(Object.isFrozen(resolvedSection.searchLabels)).toBe(true);
    expect(Object.isFrozen(resolvedWalkthrough)).toBe(true);
    expect(Object.isFrozen(resolvedWalkthrough.editions)).toBe(true);
    expect(Object.isFrozen(resolvedWalkthrough.steps)).toBe(true);
    expect(Object.isFrozen(resolvedStep)).toBe(true);
    expect(Object.isFrozen(resolvedInstruction)).toBe(true);
    expect(Object.isFrozen(resolvedInstruction?.learnMore)).toBe(true);
    expect(Object.isFrozen(resolvedStep.target)).toBe(true);
    expect(Object.isFrozen(resolvedTarget)).toBe(true);

    route.path = '/after';
    settingsSection.title = 'After';
    settingsSection.searchLabels.push('after');
    walkthrough.title = 'After';
    walkthrough.editions.push('after');
    walkthrough.steps[0].title = 'After';
    walkthrough.steps[0].instruction.learnMore.label = 'After';
    walkthrough.steps[0].target.target.id = 'After';

    expect(resolvedRoute.path).toBe('/before');
    expect(resolvedSection.title).toBe('Before');
    expect(resolvedSection.searchLabels).toEqual(['before']);
    expect(resolvedWalkthrough.title).toBe('Before');
    expect(resolvedWalkthrough.editions).toEqual(['synthetic']);
    expect(resolvedStep.title).toBe('Before');
    expect(resolvedInstruction?.learnMore?.label).toBe('Before');
    expect(resolvedTarget?.kind === 'sheet' ? resolvedTarget.id : null).toBe('Before');

    expect(Reflect.set(resolvedRoute, 'path', '/direct')).toBe(false);
    expect(Reflect.set(resolvedSection, 'title', 'Direct')).toBe(false);
    expect(Reflect.set(resolvedStep, 'title', 'Direct')).toBe(false);
  });
});
