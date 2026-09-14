// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { navigate, returnToProjectFromSettings, routePath, type Route } from '../../src/routes';
import {
  clearSettingsProjectContext,
  writeSettingsProjectContext,
} from '../../src/settings/settingsProjectContext';

const project = { id: 'sample', name: 'Sample project' };
const settings: Route = { kind: 'settings', scope: 'personal', section: 'profile' };

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, '', '/');
});

describe('returning from Settings', () => {
  it.each<Route>([
    { kind: 'project', projectId: 'sample', sheetId: '2', actionKind: 'map.ai' },
    { kind: 'project', projectId: 'sample', sheetId: '2', review: true },
    { kind: 'project', projectId: 'sample', sheetId: '2', panel: { kind: 'row', rowId: '3', columnId: 'name/notes' } },
    { kind: 'project', projectId: 'sample', sheetId: '2', panel: { kind: 'column', columnId: 'name' } },
    { kind: 'project', projectId: 'sample', sheetId: '2', panel: { kind: 'sourceHealth', sourceId: 'feed' } },
  ])('restores the project route $panel.kind $actionKind', (route) => {
    navigate(route);
    writeSettingsProjectContext(project);
    navigate(settings);
    navigate({ kind: 'settings', scope: 'personal', section: 'ai-providers' });
    navigate({ kind: 'settings', projectId: 'sample', scope: 'project', section: 'general' });
    // Project settings refresh the same project metadata without losing the origin.
    writeSettingsProjectContext(project);
    returnToProjectFromSettings('sample');
    expect(window.location.pathname).toBe(routePath(route));
  });

  it('keeps the return route when Settings reloads', async () => {
    navigate({ kind: 'project', projectId: 'sample', sheetId: '2', actionKind: 'map.ai' });
    navigate(settings);
    vi.resetModules();
    const reloadedRoutes = await import('../../src/routes');
    reloadedRoutes.returnToProjectFromSettings('sample');
    expect(window.location.pathname).toBe('/p/sample/s/2/action/map.ai');
  });

  it('returns to the chosen project when Settings changes projects', () => {
    navigate({ kind: 'project', projectId: 'sample', sheetId: '2' });
    navigate(settings);
    writeSettingsProjectContext({ id: 'other', name: 'Other project' });
    returnToProjectFromSettings('other');
    expect(window.location.pathname).toBe('/p/other');
  });

  it('falls back to the project for a direct Settings visit', () => {
    window.history.replaceState(null, '', '/p/sample/settings/project/general');
    returnToProjectFromSettings('sample');
    expect(window.location.pathname).toBe('/p/sample');
  });

  it('clears the old return route when returning through Home', () => {
    navigate({ kind: 'project', projectId: 'sample', sheetId: '2' });
    navigate(settings);
    navigate({ kind: 'picker' });
    navigate(settings);
    returnToProjectFromSettings('sample');
    expect(window.location.pathname).toBe('/p/sample');
  });

  it('clears the return route with the Home account menu project context', () => {
    navigate({ kind: 'project', projectId: 'sample', sheetId: '2' });
    navigate(settings);
    clearSettingsProjectContext();
    returnToProjectFromSettings('sample');
    expect(window.location.pathname).toBe('/p/sample');
  });
});
