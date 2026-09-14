// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { SettingsWorkspace } from '../../src/settings/SettingsWorkspace';
import { navigate } from '../../src/routes';

vi.mock('../../src/api/open', () => ({ listProjects: async () => [] }));
vi.mock('../../src/editions/module', () => ({ useEditionModule: () => ({ settingsSections: [] }) }));
vi.mock('../../src/settings/SettingsSections', () => ({
  SectionFrame: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SettingsSectionRenderer: () => null,
}));
vi.mock('../../src/workbench/ChromeBar', () => ({
  ChromeBarShell: ({ brandExtra }: { brandExtra: ReactNode }) => <header>{brandExtra}</header>,
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
});

it('the Back to project button returns to the sheet and action that opened Settings', async () => {
  window.history.replaceState(null, '', '/p/sample/s/2/action/map.ai');
  const route = { kind: 'settings', scope: 'project', projectId: 'sample', section: 'general' } as const;
  navigate(route);
  render(<SettingsWorkspace route={route} project={{ id: 'sample', name: 'Sample project' }} identityMode={false} />);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Back to Sample project' }));
  expect(window.location.pathname).toBe('/p/sample/s/2/action/map.ai');
});
