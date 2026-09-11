// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApplicationGuidance } from '../../src/components/ApplicationGuidance';
import { SampleProjectOnboarding } from '../../src/workbench/SampleProjectOnboarding';
import { TelemetryDisclosureReadyContext } from '../../src/telemetry/disclosureReady';
import { EditionModuleProvider } from '../../src/editions/module';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';
import { installDialogPolyfill } from '../support/domPolyfills';

const api = vi.hoisted(() => ({ catalog: vi.fn(), updateProfile: vi.fn() }));
vi.mock('../../src/api/walkthroughs', () => ({ listWalkthroughs: api.catalog }));
vi.mock('../../src/api/open', () => ({ updateProfile: api.updateProfile }));
vi.mock('../../src/shellIdentity', () => ({ useShellIdentity: () => ({
  resolved: true, identityMode: true, me: { email: 'reporter@example.test', cost_preapproval_usd: null },
}) }));

function Flow({ telemetryReady }: { telemetryReady: boolean }) {
  return <EditionModuleProvider edition={LOCAL_EDITION_MODULE}>
    <TelemetryDisclosureReadyContext.Provider value={telemetryReady}>
      <ApplicationGuidance projectId="demo">
        <SampleProjectOnboarding project={{ id: 'demo', name: 'Sample project' }} />
      </ApplicationGuidance>
    </TelemetryDisclosureReadyContext.Provider>
  </EditionModuleProvider>;
}

beforeEach(() => {
  installDialogPolyfill();
  localStorage.clear();
  api.catalog.mockResolvedValue({ walkthroughs: [{ id: 'regex-extract', badges: ['Backend badge'] }] });
  api.updateProfile.mockResolvedValue({ email: 'reporter@example.test', cost_preapproval_usd: '2' });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('existing first-use disclosures before sample introduction', () => {
  it('waits for telemetry and a successful cost choice, then renders backend badges', async () => {
    const view = render(<Flow telemetryReady={false} />);
    expect(screen.queryByTestId('cost-preapproval-setup')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sample-project-intro')).not.toBeInTheDocument();
    view.rerender(<Flow telemetryReady />);
    expect(screen.getByTestId('cost-preapproval-setup')).toBeInTheDocument();
    expect(screen.queryByTestId('sample-project-intro')).not.toBeInTheDocument();
    api.updateProfile.mockRejectedValueOnce(new Error('Please retry'));
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByRole('alert');
    expect(screen.queryByTestId('sample-project-intro')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    const intro = await screen.findByTestId('sample-project-intro');
    expect(screen.queryByTestId('cost-preapproval-setup')).not.toBeInTheDocument();
    expect(within(intro).getByText('Backend badge')).toBeInTheDocument();
  });

  it('keeps guides usable when descriptive metadata cannot load', async () => {
    api.catalog.mockRejectedValueOnce(new Error('offline'));
    render(<Flow telemetryReady />);
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(screen.getByTestId('sample-project-intro')).toBeVisible());
    expect(screen.getByRole('button', { name: 'Poke around first' })).toBeEnabled();
  });
});
