// @vitest-environment jsdom
//
// The Diagnostics settings section is the self-probe
// GET /api/diagnose canonical home, migrated out of DiagnosePanel.tsx's
// <dialog> into settings/SettingsSections.tsx's DiagnosticsSettings. These
// tests exercise defensive rendering against the exported settings-section
// component directly:
// the self-probe response is only type-ASSERTED (fetchDiagnostics's `as
// Promise<DiagnosticsReport>`), never runtime-validated on either side
// (src/frisket/operability/diagnostics.py's INFO wrapper returns fn() unchecked either),
// so this section must never hard-crash into the root error boundary
// (entries/mount.tsx) on a shape mismatch during reconciliation — it must
// degrade to a safe placeholder instead.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, render, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DiagnosticsSettings } from '../../src/settings/SettingsSections';



function stubDiagnoseResponse(body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      } as Response),
    ),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const CORE_OK = { store: { ok: true, detail: 'reachable' } };

describe('DiagnosticsSettings — defensive rendering of an unvalidated self-probe response', () => {
  it('renders normally when report.info.plugins is absent entirely', async () => {
    stubDiagnoseResponse({ healthy: true, core: CORE_OK, info: {} });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByTestId('diagnose-panel-status')).toHaveTextContent('Healthy');
    expect(screen.queryByText(/Plugin runtime health/)).toBeNull();
    // The rest of the report still renders — one bad/missing key must not
    // take down the whole section.
    const row = screen.getByTestId('diagnose-row-store');
    expect(within(row).getByRole('rowheader', { name: 'Store' })).toBeInTheDocument();
    expect(within(row).getByText('OK')).toHaveAttribute('data-status', 'ok');
    expect(within(row).getByText('reachable')).toBeInTheDocument();
  });

  it('does not crash when report.info.plugins is null', async () => {
    stubDiagnoseResponse({ healthy: true, core: CORE_OK, info: { plugins: null } });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByText(/Plugin runtime health/)).toBeInTheDocument();
    // infoLine(null) degrades to an em dash placeholder, not a thrown
    // TypeError from `null.summary`.
    const row = screen.getByTestId('diagnose-row-plugins');
    expect(within(row).getByRole('rowheader', { name: 'Plugin runtime health' })).toBeInTheDocument();
    expect(within(row).getByText('Info')).toHaveAttribute('data-status', 'info');
    expect(within(row).getByText('—')).toBeInTheDocument();
  });

  it('does not crash when report.info itself is null', async () => {
    stubDiagnoseResponse({ healthy: false, core: CORE_OK, info: null });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByTestId('diagnose-panel-status')).toHaveTextContent('Unhealthy');
    // Object.entries(null) would throw; safeEntries must have absorbed it.
    expect(screen.queryByText(/Plugin runtime health/)).toBeNull();
  });

  it('does not crash when report.info is missing (undefined) entirely', async () => {
    stubDiagnoseResponse({ healthy: true, core: CORE_OK });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByTestId('diagnose-panel-status')).toHaveTextContent('Healthy');
  });

  it('does not crash and falls back to JSON when plugins is a shape without a summary field', async () => {
    stubDiagnoseResponse({
      healthy: true,
      core: CORE_OK,
      info: { plugins: { installed: 3, active: 2 } },
    });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByText(/Plugin runtime health/)).toBeInTheDocument();
    expect(screen.getByTestId('diagnose-panel-body')).toHaveTextContent('"installed":3');
  });

  it('does not crash when plugins is a bare string instead of an object', async () => {
    stubDiagnoseResponse({ healthy: true, core: CORE_OK, info: { plugins: 'unexpected-shape' } });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByTestId('diagnose-row-plugins')).toHaveTextContent('unexpected-shape');
  });

  it('renders the plugin-health shape via the summary field', async () => {
    stubDiagnoseResponse({
      healthy: true,
      core: CORE_OK,
      info: {
        plugins: {
          available: true,
          installed: 3,
          active: 2,
          failed: 1,
          failed_names: ['frisket.broken'],
          summary: '3 installed, 2 active, 1 failed (frisket.broken)',
        },
      },
    });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByTestId('diagnose-row-plugins')).toHaveTextContent(
      '3 installed, 2 active, 1 failed (frisket.broken)',
    );
  });

  it('separates failed, healthy, and informational checks into visible statuses', async () => {
    stubDiagnoseResponse({
      healthy: false,
      core: {
        store: { ok: true, detail: 'reachable' },
        model_runtime: { ok: false, error: 'connection refused' },
        malformed_probe: { detail: 'missing verdict' },
      },
      info: { local_engines: { summary: '2 available' } },
    });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-table')).toBeInTheDocument());

    expect(within(screen.getByTestId('diagnose-row-store')).getByText('OK'))
      .toHaveAttribute('data-status', 'ok');
    const failed = screen.getByTestId('diagnose-row-model_runtime');
    expect(within(failed).getByText('Failed')).toHaveAttribute('data-status', 'failed');
    expect(failed).toHaveTextContent('connection refused');
    expect(within(screen.getByTestId('diagnose-row-malformed_probe')).getByText('Unknown'))
      .toHaveAttribute('data-status', 'unknown');
    expect(within(screen.getByTestId('diagnose-row-local_engines')).getByText('Info'))
      .toHaveAttribute('data-status', 'info');
  });

  it('does not crash when report.core is malformed (a non-object value)', async () => {
    stubDiagnoseResponse({ healthy: false, core: 'not-an-object', info: {} });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByTestId('diagnose-panel-status')).toHaveTextContent('Unhealthy');
  });

  it('Refresh re-runs the self-probe', async () => {
    const user = userEvent.setup();
    stubDiagnoseResponse({ healthy: true, core: CORE_OK, info: {} });
    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(fetch).toHaveBeenCalledTimes(1);

    await user.click(screen.getByTestId('settings-diagnostics-refresh'));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });

  // A failed Refresh used to render the previous
  // report's "Healthy" status with no indication it was stale — the only
  // sign anything went wrong was a separate error banner above it, easy to
  // miss or read as unrelated. A failed refresh must never look like a fresh
  // "all good" verdict.
  it('a Refresh that fails after a successful load labels the report stale instead of showing it as current', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ healthy: true, core: CORE_OK, info: {} }),
      } as Response)
      .mockResolvedValueOnce({
        ok: false,
        status: 503,
        json: () => Promise.resolve({}),
      } as Response);
    vi.stubGlobal('fetch', fetchMock);

    render(<DiagnosticsSettings />);
    await waitFor(() => expect(screen.getByTestId('diagnose-panel-body')).toBeInTheDocument());
    expect(screen.getByTestId('diagnose-panel-status')).toHaveTextContent('Healthy');
    expect(screen.queryByTestId('diagnose-panel-stale-note')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('settings-diagnostics-refresh'));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());

    // The stale report is still visible (not blanked out) but now visibly
    // marked as last-known rather than current.
    expect(screen.getByTestId('diagnose-panel-stale-note')).toBeVisible();
    expect(screen.getByTestId('diagnose-panel-status')).toHaveTextContent('Last known: Healthy');
  });
});
