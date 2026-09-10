// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import {
  defineEditionModule,
  EditionModuleProvider,
} from '../../src/editions/module';
import { SettingsWorkspace } from '../../src/settings/SettingsWorkspace';

const listProjects = vi.hoisted(() => vi.fn());

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  listProjects,
}));

vi.mock('../../src/workbench/ChromeBar', () => ({
  ChromeBarShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllEnvs();
  localStorage.clear();
});

it('hides notification navigation and refuses its deep link when destinations are managed', () => {
  const editionId = 'managed-destination-settings-browser';
  const edition = defineEditionModule({
    descriptor: {
      id: editionId,
      capabilities: {
        configurableNotificationDestinations: false,
        configurableNotificationEmail: false,
        identity: true,
        team: true,
      },
    },
  });
  listProjects.mockResolvedValue([]);

  render(
    <EditionModuleProvider edition={edition}>
      <SettingsWorkspace
        route={{
          kind: 'settings',
          scope: 'project',
          section: 'notifications',
          projectId: 'project-1',
        }}
        project={{ id: 'project-1', name: 'Managed project' }}
        identityMode
      />
    </EditionModuleProvider>,
  );

  expect(screen.queryByTestId('settings-nav-project-notifications')).not.toBeInTheDocument();
  expect(screen.getByTestId('settings-invalid-section')).toHaveTextContent(
    'project.notifications is not available',
  );
  expect(screen.queryByTestId('notification-settings-panel')).not.toBeInTheDocument();
});
