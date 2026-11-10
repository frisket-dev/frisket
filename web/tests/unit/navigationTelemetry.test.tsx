// @vitest-environment jsdom
import { type PropsWithChildren } from 'react';
import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { TelemetryDisclosureReadyContext } from '../../src/telemetry/disclosureReady';
import { configureProductTelemetry, setTelemetryPreference } from '../../src/telemetry/productTelemetry';
import { telemetryPanelKind, useSettingsNavigationTelemetry, useWorkspaceNavigationTelemetry } from '../../src/telemetry/navigationTelemetry';

const request = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
const wrapper = ({ children }: PropsWithChildren) => <TelemetryDisclosureReadyContext.Provider value>{children}</TelemetryDisclosureReadyContext.Provider>;
const events = () => request.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));

beforeEach(() => {
  localStorage.clear();
  configureProductTelemetry(true);
  setTelemetryPreference(true);
  request.mockClear();
  vi.stubGlobal('fetch', request);
});
afterEach(() => { cleanup(); configureProductTelemetry(false); vi.unstubAllGlobals(); });

it('observes visible navigation once, including close/reopen, without rerender or reorder noise', () => {
  type Props = Parameters<typeof useWorkspaceNavigationTelemetry>[0];
  const initial: Props = { projectId: 'project-a', view: 'document', rightPanels: ['filter', 'sources'], bottomPanel: 'history' };
  const { rerender } = renderHook((props: Props) => useWorkspaceNavigationTelemetry(props), { initialProps: initial, wrapper });
  expect(events().map(e => e.type)).toEqual(['Workspace.viewOpened', 'Workspace.panelOpened', 'Workspace.panelOpened', 'Workspace.panelOpened']);
  rerender({ ...initial, rightPanels: ['sources', 'filter'] });
  expect(request).toHaveBeenCalledTimes(4);
  rerender({ ...initial, rightPanels: [], bottomPanel: null });
  expect(request).toHaveBeenCalledTimes(4);
  rerender({ ...initial, rightPanels: ['filter'], bottomPanel: null });
  expect(events().at(-1)?.properties).toEqual({ region: 'right', panel: 'filter' });
  expect(request).toHaveBeenCalledTimes(5);
  rerender({ ...initial, view: 'grid', rightPanels: ['filter'], bottomPanel: null });
  expect(events().at(-1)?.properties).toEqual({ view: 'grid' });
  rerender({ projectId: 'project-b', view: 'grid', rightPanels: ['filter'], bottomPanel: null });
  expect(request).toHaveBeenCalledTimes(8);
  expect(JSON.stringify(events())).not.toContain('project-a');
});

it('waits for disclosure readiness before recording the initial view', () => {
  let ready = false;
  const { rerender } = renderHook(() => useWorkspaceNavigationTelemetry({ projectId: 'p', view: 'grid' }), {
    wrapper: ({ children }: PropsWithChildren) => <TelemetryDisclosureReadyContext.Provider value={ready}>{children}</TelemetryDisclosureReadyContext.Provider>,
  });
  expect(request).not.toHaveBeenCalled();
  ready = true;
  rerender();
  expect(events().map(e => e.type)).toEqual(['Workspace.viewOpened']);
});

it('keeps opt-out and missing project contexts silent', () => {
  setTelemetryPreference(false);
  renderHook(() => useWorkspaceNavigationTelemetry({ projectId: 'p', view: 'document', rightPanels: ['filter'] }), { wrapper });
  renderHook(() => useSettingsNavigationTelemetry({ scope: 'personal', sectionId: 'personal.privacy', available: true }), { wrapper });
  expect(request).not.toHaveBeenCalled();
  setTelemetryPreference(true);
  renderHook(() => useWorkspaceNavigationTelemetry({ projectId: null, view: 'document' }), { wrapper });
  expect(request).not.toHaveBeenCalled();
});

it('maps builtin panels and custom panels without forwarding extension identifiers', () => {
  expect(telemetryPanelKind('frisket.investigative.panel.friendly_filters')).toBe('filter');
  expect(telemetryPanelKind('frisket.investigative.panel.mentions')).toBe('mentions');
  expect(telemetryPanelKind('frisket.core.panel.sources')).toBe('sources');
  expect(telemetryPanelKind('frisket.core.panel.history')).toBe('history');
  expect(telemetryPanelKind('private-org.secret-plugin.panel')).toBe('plugin');
});

it('only reports available settings and sends project settings through the project endpoint', () => {
  type Props = Parameters<typeof useSettingsNavigationTelemetry>[0];
  const initial: Props = { scope: 'personal', sectionId: 'personal.privacy', available: false };
  const { rerender } = renderHook((props: Props) => useSettingsNavigationTelemetry(props), { initialProps: initial, wrapper });
  expect(request).not.toHaveBeenCalled();
  rerender({ ...initial, available: true });
  expect(events().at(-1)?.properties).toEqual({ scope: 'personal', section: 'privacy' });
  rerender({ ...initial, available: true });
  expect(request).toHaveBeenCalledTimes(1);
  rerender({ scope: 'organization', sectionId: 'organization.modelsGateway', available: true });
  expect(events().at(-1)?.properties).toEqual({ scope: 'organization', section: 'models_gateway' });
  rerender({ scope: 'project', sectionId: 'project.general', projectId: 'private-project', available: true });
  expect(String(request.mock.calls.at(-1)?.[0])).toContain('/api/projects/private-project/telemetry/events');
  expect(events().at(-1)?.type).toBe('Project.settingsSectionOpened');
  expect(events().at(-1)?.properties).toEqual({ section: 'general' });
  rerender({ scope: 'personal', sectionId: 'private.custom-settings', available: true });
  expect(events().at(-1)?.properties).toEqual({ scope: 'personal', section: 'extension' });
  expect(JSON.stringify(events())).not.toContain('private.');
});
