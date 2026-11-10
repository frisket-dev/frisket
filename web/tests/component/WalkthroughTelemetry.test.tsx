// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { EditionModuleProvider } from '../../src/editions/module';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';
import { TelemetryDisclosureReadyContext } from '../../src/telemetry/disclosureReady';
import { configureProductTelemetry, sendProductTelemetry, setTelemetryPreference } from '../../src/telemetry/productTelemetry';
import { WalkthroughProvider } from '../../src/walkthrough/WalkthroughProvider';
import { useWalkthrough } from '../../src/walkthrough/context';
import { REGEX_EXTRACT_WALKTHROUGH } from '../../src/walkthrough/walkthroughs';

const request = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
const events = () => request.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
function Controls({ pid }: { pid: string }) {
  const guide = useWalkthrough();
  return <>
    <button onClick={() => guide.startWalkthrough('regex-extract')}>Start test</button>
    <button onClick={guide.openWalkthroughChooser}>Choose test</button>
    <button onClick={guide.resumeWalkthrough}>Resume test</button>
    <button onClick={() => sendProductTelemetry({ type: 'Action.opened', properties: { action: 'map.extract' } }, pid)}>Action test</button>
  </>;
}
function App({ pid = 'p', ready = true }: { pid?: string; ready?: boolean }) {
  return <EditionModuleProvider edition={LOCAL_EDITION_MODULE}>
    <TelemetryDisclosureReadyContext.Provider value={ready}>
      <WalkthroughProvider projectId={pid}><Controls pid={pid} /></WalkthroughProvider>
    </TelemetryDisclosureReadyContext.Provider>
  </EditionModuleProvider>;
}
beforeEach(() => {
  localStorage.clear(); request.mockClear();
  configureProductTelemetry(true); setTelemetryPreference(true);
  vi.stubGlobal('fetch', request);
});
afterEach(() => { cleanup(); configureProductTelemetry(false); vi.unstubAllGlobals(); });
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name, exact: true }));

it('tracks actual guide steps, pause/resume and completion; attributes action activity', () => {
  const view = render(<App />);
  click('Choose test');
  click('Action test');
  expect(events().at(-1).properties.walkthroughState).toBe('none');
  click('Start test');
  expect(events().at(-1)).toMatchObject({ type: 'Walkthrough.stepViewed', properties: { walkthrough: 'regex-extract', step: '1' } });
  const count = request.mock.calls.length;
  view.rerender(<App />);
  expect(request.mock.calls).toHaveLength(count);
  click('Action test');
  expect(events().at(-1).properties).toMatchObject({ walkthrough: 'regex-extract', walkthroughState: 'active' });
  click('Next');
  expect(events().at(-1).properties.step).toBe('2');
  click('Pause walkthrough');
  click('Action test');
  expect(events().at(-1).properties.walkthroughState).toBe('paused');
  click('Resume test');
  expect(events().at(-1).properties.step).toBe('2');
  for (let i = 2; i < REGEX_EXTRACT_WALKTHROUGH.steps.length; i++) click('Next');
  click('Finish');
  expect(events().at(-1).type).toBe('Walkthrough.completed');
  click('Action test');
  expect(events().at(-1).properties.walkthroughState).toBe('none');
});

it('waits for disclosure and isolates project switches; opt-out stays silent', () => {
  const view = render(<App ready={false} />);
  click('Start test');
  expect(events()).toEqual([]);
  view.rerender(<App />);
  expect(events()).toHaveLength(1);
  view.rerender(<App pid="other-project" />);
  click('Action test');
  expect(events().at(-1).properties.walkthroughState).toBe('none');
  setTelemetryPreference(false);
  const count = request.mock.calls.length;
  click('Start test'); click('Action test');
  expect(events()).toHaveLength(count);
});
