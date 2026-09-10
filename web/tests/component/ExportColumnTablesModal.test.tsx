// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ActionCatalogEntry,
  ProjectInfo,
  RunActionLaunchResult,
  SheetMeta,
  V1Receipt,
} from '../../src/api/types';
import type { ProjectApiPort } from '../../src/api/ports';


import { ProjectExportModals } from '../../src/components/TopNav';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectA: ProjectInfo = { id: 'project-a', name: 'Project A' };
const projectB: ProjectInfo = { id: 'project-b', name: 'Project B' };
const api = createProjectApi(projectA.id);
const { render } = createWorkspaceTestHarness({
  projectId: projectA.id,
  api: { projectApi: api },
});
const projectApi = {} as unknown as ProjectApiPort;
const currentSheet: SheetMeta = {
  id: '7',
  name: 'Source data',
  rowCount: 1,
  columns: [{ id: '9', name: 'tables', type: 'json' }],
};
const catalogEntry = {
  kind: 'export.column_tables',
  input_schema: { properties: {} },
} as unknown as ActionCatalogEntry;

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function receipt(status: string): V1Receipt {
  return {
    schemaVersion: 'frisket.receipt.v1',
    receiptId: 'receipt-a',
    projectId: projectA.id,
    actionId: 'action-a',
    actionKind: 'export.column_tables',
    runId: null,
    opIds: [],
    idempotencyKey: 'key-a',
    paramsHash: 'hash-a',
    status,
    inputs: [],
    outputs: [
      {
        kind: 'export',
        name: 'column_tables',
        ref: {
          blob_hash: 'sha256:export-a',
          project_path: 'exports/tables/project-a.zip',
        },
      },
    ],
    providerUse: [],
    evidence: [],
    errors: [],
  } as unknown as V1Receipt;
}

function modal(project: ProjectInfo) {
  return (
    <ProjectExportModals
      modal="column_tables"
      project={project}
      projectApi={projectApi}
      currentSheet={currentSheet}
      columnTablesExportEntry={catalogEntry}
      onClose={() => {}}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('ExportColumnTablesModal project capture', () => {
  it('keeps a receipt-only export and ZIP download bound to the submitting project after a switch', async () => {
    vi.useFakeTimers();
    const launch = deferred<RunActionLaunchResult>();
    const runAction = vi.spyOn(api, 'runAction').mockReturnValue(launch.promise);
    const getReceipt = vi.spyOn(api, 'getReceipt')
      .mockResolvedValueOnce(receipt('running'))
      .mockResolvedValueOnce(receipt('completed'));
    const { rerender } = render(modal(projectA));

    fireEvent.change(screen.getByTestId('export-column-tables-group-by'), {
      target: { value: ' table_index ' },
    });
    fireEvent.change(screen.getByTestId('export-column-tables-exclude-columns'), {
      target: { value: ' debug, \n source_filename \n' },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('export-column-tables-submit'));
    });
    expect(runAction).toHaveBeenCalledTimes(1);

    rerender(modal(projectB));

    await act(async () => {
      launch.resolve({ runId: null, receiptId: 'receipt-a', status: 'queued' });
      await Promise.resolve();
    });
    expect(getReceipt).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });

    const download = screen.getByTestId('export-column-tables-download');
    expect(download).toHaveAttribute(
      'href',
      '/api/projects/project-a/blobs/sha256%3Aexport-a',
    );
    expect(runAction).toHaveBeenLastCalledWith(
      {
        action_id: 'export.column_tables',
        scope: { kind: 'project' },
        params: {
          sheet_id: 7,
          column_id: 9,
          destination: { kind: 'project_file', prefix: 'exports/tables' },
          group_by: 'table_index',
          exclude_columns: ['debug', 'source_filename'],
        },
        output_names: {},
        idempotency_key: expect.stringMatching(/^web-export\.column_tables:/),
      },
      { projectId: projectA.id },
    );
    expect(getReceipt).toHaveBeenNthCalledWith(1, 'receipt-a', { projectId: projectA.id });
    expect(getReceipt).toHaveBeenNthCalledWith(2, 'receipt-a', { projectId: projectA.id });
  });
});
