import { receiptOutputsAsActionOutputs } from './actionCompletion';
import {
  ApiError,
  ConfirmationRequiredError,
  v1ErrorMessage,
} from './contractErrors';
import { getReceiptContract } from './httpContractRoutes';
import type {
  GoogleSheetsExportInput,
  GoogleSheetsExportResult,
  JsonValue,
} from './types';
import type {
  V1ActionResult,
  V1ActionSession,
} from './v1ActionSession';

const GOOGLE_SHEETS_EXPORT_POLL_INTERVAL_MS = 1000;
const GOOGLE_SHEETS_EXPORT_MAX_POLLS = 300;

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface GoogleSheetsExportApi {
  exportGoogleSheets(input: GoogleSheetsExportInput): Promise<GoogleSheetsExportResult>;
}

export interface GoogleSheetsExportDependencies {
  v1ActionSession: Pick<
    V1ActionSession,
    | 'clearV1ActionIdempotencyKey'
    | 'postV1ActionSpec'
    | 'releaseV1ActionIdempotencyKey'
    | 'registeredProjectActionSpec'
  >;
}

function isTerminalReceiptStatus(
  status: string,
): status is 'completed' | 'partial' | 'failed' | 'cancelled' {
  return ['completed', 'partial', 'failed', 'cancelled'].includes(status);
}

function waitForPollInterval(): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, GOOGLE_SHEETS_EXPORT_POLL_INTERVAL_MS);
  });
}

function exportSource(input: GoogleSheetsExportInput): Record<string, unknown> {
  if (input.sourceKind === 'all_sheets') return { kind: 'all_sheets' };
  const sheetId = input.sheetId ? Number(input.sheetId) : NaN;
  if (!Number.isFinite(sheetId)) throw new ApiError(400, 'Choose a sheet to export');
  if (input.sourceKind === 'current_view') {
    const filter = input.currentView?.filter ?? null;
    const sort = input.currentView?.sort ?? null;
    return {
      kind: 'current_view',
      sheet_id: sheetId,
      query: {
        schema_version: 'frisket.query.v1',
        kind: 'sheet.filter',
        scope: { kind: 'sheet', sheet_id: sheetId },
        ...(filter ? { filter } : {}),
        ...(sort && sort.length > 0 ? { sort } : {}),
      },
    };
  }
  return { kind: 'current_sheet', sheet_id: sheetId };
}

function exportDestination(input: GoogleSheetsExportInput): Record<string, unknown> {
  if (input.destinationKind === 'update_existing') {
    const spreadsheetId = (input.spreadsheetId ?? '').trim();
    if (!spreadsheetId) throw new ApiError(400, 'Enter a spreadsheet ID');
    return {
      kind: 'google_sheets',
      mode: 'update_existing',
      spreadsheet_id: spreadsheetId,
    };
  }
  return {
    kind: 'google_sheets',
    mode: 'new_spreadsheet',
    spreadsheet_title: input.spreadsheetTitle?.trim() || 'Frisket export',
  };
}

function exportParams(input: GoogleSheetsExportInput): Record<string, unknown> {
  const connectionId = input.connectionId.trim();
  if (!connectionId) throw new ApiError(400, 'Choose a Google connection');
  return {
    connection_id: connectionId,
    source: exportSource(input),
    destination: exportDestination(input),
    write_policy: 'replace_managed_tabs',
  };
}

export function createGoogleSheetsExportApi(
  errorFactory: ContractErrorFactory,
  dependencies: GoogleSheetsExportDependencies,
  projectId: string,
): GoogleSheetsExportApi {
  async function waitForCompletion(
    result: V1ActionResult,
    projectId: string,
    attempt = 0,
  ): Promise<V1ActionResult> {
    if (isTerminalReceiptStatus(result.status)) return result;
    if (result.status !== 'queued' && result.status !== 'running') {
      throw new ApiError(
        500,
        v1ErrorMessage(result, `Google Sheets export returned ${result.status}`),
      );
    }
    if (!result.receipt_id) {
      throw new ApiError(500, 'Google Sheets export: queued action did not return a receipt');
    }
    if (attempt >= GOOGLE_SHEETS_EXPORT_MAX_POLLS) {
      throw new ApiError(504, 'Google Sheets export: timed out waiting for completion');
    }

    const receipt = await getReceiptContract(projectId, result.receipt_id, errorFactory);
    if (receipt.status === 'queued' || receipt.status === 'running') {
      await waitForPollInterval();
      return waitForCompletion(result, projectId, attempt + 1);
    }
    if (!isTerminalReceiptStatus(receipt.status)) {
      throw new ApiError(500, `Google Sheets export returned ${receipt.status}`);
    }
    return {
      ...result,
      status: receipt.status,
      outputs: receiptOutputsAsActionOutputs(receipt),
      errors: receipt.errors.map((error) => ({
        code: error.code,
        message: error.message,
      })),
    };
  }

  return {
    async exportGoogleSheets(input) {
      const params = exportParams(input);
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'export.google_sheets',
        params as Record<string, JsonValue>,
        projectId,
      );
      if (input.confirmation) spec.confirmation = input.confirmation;
      let result: V1ActionResult;
      try {
        result = await dependencies.v1ActionSession.postV1ActionSpec(spec, { projectId });
      } catch (error) {
        // A 402 pauses this exact attempt; every other pre-acceptance failure
        // clears the key so the next submit is a fresh attempt.
        if (!(error instanceof ConfirmationRequiredError)) {
          dependencies.v1ActionSession.clearV1ActionIdempotencyKey(spec);
        }
        throw error;
      }

      const settled = await waitForCompletion(result, projectId);
      if (settled.status !== 'completed') {
        if (isTerminalReceiptStatus(settled.status)) {
          dependencies.v1ActionSession.clearV1ActionIdempotencyKey(spec);
        }
        throw new ApiError(400, v1ErrorMessage(settled, 'Google Sheets export failed'));
      }

      // A completed server attempt keeps the ordinary replay window even if
      // its receipt is malformed, so the browser never retains a poisoned key.
      dependencies.v1ActionSession.releaseV1ActionIdempotencyKey(spec);
      const output = settled.outputs?.find((candidate) => candidate.name === 'google_sheets');
      const outputRef = output?.ref;
      const spreadsheetId = outputRef?.spreadsheet_id;
      if (
        !output
        || outputRef === undefined
        || typeof spreadsheetId !== 'string'
        || spreadsheetId.trim().length === 0
      ) {
        throw new ApiError(500, 'Google Sheets export completed without a spreadsheet id');
      }
      return {
        spreadsheetId,
        spreadsheetUrl:
          typeof outputRef.spreadsheet_url === 'string'
            ? outputRef.spreadsheet_url
            : null,
        updatedTabs: Array.isArray(outputRef.updated_tabs)
          ? outputRef.updated_tabs.filter((item): item is Record<string, unknown> =>
              item !== null && typeof item === 'object' && !Array.isArray(item),
            )
          : [],
        receiptId: settled.receipt_id,
      };
    },
  };
}
