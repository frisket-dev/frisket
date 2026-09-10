// @vitest-environment jsdom
//
// Bug: export.google_sheets is registered unconditionally in the action
// catalog, but only actually runs when the composition wired a connected-
// account resolver (ExecutorDeps.connected_account_resolver -- see
// tests/server/test_google_sheets_connected_account_hint.py and
// tests/team/test_google_sheets_executor_wiring.py for the backend half).
// The bare local single-user tier never does, so /api/org/oauth/connections
// and /api/org/oauth/google/start both 404 there -- before this fix, the
// modal caught that failure and still rendered a "Connect Google" link
// (guaranteed to 404 too) alongside a raw fetch-error string. This proves
// the modal instead reads the SAME server-truthful catalog hint
// (ui_hints.unavailable_reason) ActionPanel's missing_credentials/
// network_disabled gates already use, and shows one clear reason with no
// dead-end affordance.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActionCatalogPayload, ProjectInfo, SheetMeta } from '../../src/api/types';
import type { ProjectApiPort } from '../../src/api/ports';

import { ConfirmationRequiredError } from '../../src/api/open';
import { ProjectExportModals } from '../../src/components/TopNav';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project',
  api: { projectApi: api },
});

afterEach(cleanup);
beforeEach(() => {
  vi.restoreAllMocks();
});

const project: ProjectInfo = { id: 'proj-1', name: 'Sheets Desk' };
const fakeProjectApi = {} as unknown as ProjectApiPort;
const currentSheet: SheetMeta = {
  id: '7',
  name: 'People',
  rowCount: 3,
  columns: [{ id: '1', name: 'name', type: 'text' }],
};

function catalogWithReason(reason: string | null): ActionCatalogPayload {
  return {
    actions: [
      {
        kind: 'export.google_sheets',
        ui_hints: reason ? { unavailable_reason: reason } : {},
      },
    ],
  } as unknown as ActionCatalogPayload;
}

describe('ExportGoogleSheetsModal availability gate (bugfix)', () => {
  it('shows the catalog-declared reason instead of a dead-end "Connect Google" link', async () => {
    vi.spyOn(api, 'listActionCatalog').mockResolvedValue(
      catalogWithReason(
        'Google Sheets export needs a connected Google account, and this single-user tier has no OAuth flow to connect one. Available on the Team edition.',
      ),
    );
    // Even if this resolves/rejects, the reason above must win visually.
    vi.spyOn(api, 'listOAuthConnections').mockRejectedValue(new Error('Not Found'));
    const exportSpy = vi.spyOn(api, 'exportGoogleSheets');

    render(
      <ProjectExportModals
        modal="google_sheets"
        project={project}
        projectApi={fakeProjectApi}
        onClose={() => {}}
      />,
    );

    const notice = await screen.findByTestId('export-google-sheets-unavailable');
    expect(notice).toHaveTextContent(/team edition/i);
    expect(screen.queryByText('Connect Google')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Export$/ })).not.toBeInTheDocument();
    expect(exportSpy).not.toHaveBeenCalled();
  });

  it('falls through to the normal connections flow when the action is available', async () => {
    vi.spyOn(api, 'listActionCatalog').mockResolvedValue(catalogWithReason(null));
    vi.spyOn(api, 'listOAuthConnections').mockResolvedValue([
      { id: 'conn-1', connection_id: 'conn-1', external_email: 'reporter@example.com' },
    ] as never);

    render(
      <ProjectExportModals
        modal="google_sheets"
        project={project}
        projectApi={fakeProjectApi}
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText('reporter@example.com')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('export-google-sheets-unavailable')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Export$/ })).toBeInTheDocument();
  });

  it('degrades to the pre-existing behavior if the catalog fetch itself fails', async () => {
    vi.spyOn(api, 'listActionCatalog').mockRejectedValue(new Error('network down'));
    vi.spyOn(api, 'listOAuthConnections').mockResolvedValue([]);

    render(
      <ProjectExportModals
        modal="google_sheets"
        project={project}
        projectApi={fakeProjectApi}
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText('Connect Google')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('export-google-sheets-unavailable')).not.toBeInTheDocument();
  });
});

describe('ExportGoogleSheetsModal confirmation admission', () => {
  function arrangeAvailableExport() {
    vi.spyOn(api, 'listActionCatalog').mockResolvedValue(catalogWithReason(null));
    vi.spyOn(api, 'listOAuthConnections').mockResolvedValue([
      { id: 'conn-1', connection_id: 'conn-1', external_email: 'first@example.com' },
      { id: 'conn-2', connection_id: 'conn-2', external_email: 'second@example.com' },
    ] as never);
  }

  function renderModal() {
    return render(
      <ProjectExportModals
        modal="google_sheets"
        project={project}
        projectApi={fakeProjectApi}
        currentSheet={currentSheet}
        currentSheetExportOptions={{
          filter: { city: { eq: 'Paris' } },
          sort: [{ column: 'name', dir: 'asc' }],
        }}
        onClose={() => {}}
      />,
    );
  }

  it('renders server claims, freezes the normalized first submission, and retries its exact hash', async () => {
    arrangeAvailableExport();
    const exportSpy = vi
      .spyOn(api, 'exportGoogleSheets')
      .mockRejectedValueOnce(new ConfirmationRequiredError({
        cost: null,
        rows: 0,
        claims: [
          { field: 'egress_class', display: 'Data is sent to Google Sheets.' },
          {
            field: 'irreversible_external',
            display: 'Frisket cannot undo the external spreadsheet write.',
          },
        ],
        promise_set_hash: 'sha256:google-modal-1',
      }, 'Google Sheets may create or replace tabs.', 'irreversible_external'))
      .mockResolvedValueOnce({
        spreadsheetId: 'spreadsheet-1',
        spreadsheetUrl: 'https://docs.google.test/spreadsheet-1',
        updatedTabs: [],
        receiptId: 'receipt-1',
      });

    renderModal();
    const account = await screen.findByRole('combobox', { name: 'Google account' });
    fireEvent.change(account, { target: { value: 'conn-2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Current view' }));
    fireEvent.click(screen.getByRole('button', { name: 'Update existing spreadsheet' }));
    const spreadsheetId = screen.getByRole('textbox', { name: 'Spreadsheet ID' });
    fireEvent.change(spreadsheetId, { target: { value: '  spreadsheet-original  ' } });
    fireEvent.click(screen.getByTestId('export-google-sheets-submit'));

    expect(await screen.findByTestId('export-google-sheets-confirmation-message'))
      .toHaveTextContent('Google Sheets may create or replace tabs.');
    expect(screen.getByTestId('export-google-sheets-confirmation-claims'))
      .toHaveTextContent('Data is sent to Google Sheets.');
    expect(screen.getByTestId('export-google-sheets-confirmation-claims'))
      .toHaveTextContent('Frisket cannot undo the external spreadsheet write.');

    const frozenInput = {
      connectionId: 'conn-2',
      sourceKind: 'current_view',
      sheetId: '7',
      currentView: {
        filter: { city: { eq: 'Paris' } },
        sort: [{ column: 'name', dir: 'asc' }],
      },
      destinationKind: 'update_existing',
      spreadsheetTitle: 'Sheets Desk export',
      spreadsheetId: 'spreadsheet-original',
    };
    expect(exportSpy).toHaveBeenNthCalledWith(1, frozenInput);
    expect(account).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Current view' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Update existing spreadsheet' })).toBeDisabled();
    expect(spreadsheetId).toBeDisabled();

    // Even a synthetic change event cannot alter the by-value retry snapshot.
    fireEvent.change(spreadsheetId, { target: { value: 'tampered-after-challenge' } });
    const confirmationInput = screen.getByTestId('export-google-sheets-confirmation-input');
    fireEvent.change(confirmationInput, { target: { value: 'Confirm' } });
    expect(screen.getByTestId('export-google-sheets-submit')).toBeDisabled();
    fireEvent.submit(confirmationInput.closest('form')!);
    expect(exportSpy).toHaveBeenCalledTimes(1);
    fireEvent.change(confirmationInput, { target: { value: 'confirm' } });
    fireEvent.click(screen.getByTestId('export-google-sheets-submit'));

    await waitFor(() => expect(exportSpy).toHaveBeenCalledTimes(2));
    expect(exportSpy).toHaveBeenNthCalledWith(2, {
      ...frozenInput,
      confirmation: 'sha256:google-modal-1',
    });
    expect(await screen.findByRole('link', { name: 'Open spreadsheet' }))
      .toHaveAttribute('href', 'https://docs.google.test/spreadsheet-1');
  });

  it('replaces a changed challenge and clears typed confirmation before the exact retry', async () => {
    arrangeAvailableExport();
    const exportSpy = vi
      .spyOn(api, 'exportGoogleSheets')
      .mockRejectedValueOnce(new ConfirmationRequiredError({
        cost: null,
        rows: 0,
        claims: [{ field: 'egress_class', display: 'First server claim.' }],
        promise_set_hash: 'sha256:google-modal-old',
      }, 'First challenge', 'irreversible_external'))
      .mockRejectedValueOnce(new ConfirmationRequiredError({
        cost: null,
        rows: 0,
        claims: [{ field: 'egress_class', display: 'Replacement server claim.' }],
        promise_set_hash: 'sha256:google-modal-new',
      }, 'Challenge changed', 'irreversible_external'))
      .mockResolvedValueOnce({
        spreadsheetId: 'spreadsheet-2',
        spreadsheetUrl: null,
        updatedTabs: [],
        receiptId: 'receipt-2',
      });

    renderModal();
    await screen.findByText('first@example.com');
    fireEvent.click(screen.getByTestId('export-google-sheets-submit'));
    await screen.findByText('First challenge');

    let confirmationInput = screen.getByTestId('export-google-sheets-confirmation-input');
    fireEvent.change(confirmationInput, { target: { value: 'confirm' } });
    fireEvent.click(screen.getByTestId('export-google-sheets-submit'));

    await screen.findByText('Challenge changed');
    expect(screen.queryByText('First server claim.')).not.toBeInTheDocument();
    expect(screen.getByText('Replacement server claim.')).toBeInTheDocument();
    confirmationInput = screen.getByTestId('export-google-sheets-confirmation-input');
    expect(confirmationInput).toHaveValue('');
    expect(screen.getByTestId('export-google-sheets-submit')).toBeDisabled();

    fireEvent.change(confirmationInput, { target: { value: 'confirm' } });
    fireEvent.click(screen.getByTestId('export-google-sheets-submit'));
    await waitFor(() => expect(exportSpy).toHaveBeenCalledTimes(3));

    const firstInput = exportSpy.mock.calls[0]![0];
    expect(exportSpy).toHaveBeenNthCalledWith(2, {
      ...firstInput,
      confirmation: 'sha256:google-modal-old',
    });
    expect(exportSpy).toHaveBeenNthCalledWith(3, {
      ...firstInput,
      confirmation: 'sha256:google-modal-new',
    });
  });
});
