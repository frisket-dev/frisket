// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { V1Receipt } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ReceiptInspector } from '../../src/components/ReceiptInspector';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';

const digest = 'a'.repeat(64);
let stores: WorkspaceStores;

beforeEach(() => {
  stores = createWorkspaceStores('current-project', createProjectApi('current-project'));
});
afterEach(() => {
  cleanup();
  stores.dispose();
  vi.restoreAllMocks();
});

async function inspect(ref: Record<string, unknown>, overrides: Partial<V1Receipt> = {}) {
  const receipt: V1Receipt = {
    schemaVersion: 'frisket.receipt.v1', receiptId: 'receipt-export',
    projectId: 'source/project ?', actionId: 'action-export', actionKind: 'export.column_tables',
    runId: null, opIds: [], idempotencyKey: null, paramsHash: null, status: 'completed',
    inputs: [], outputs: [{ name: 'package', ref }], providerUse: [], evidence: [], errors: [],
    ...overrides,
  };
  vi.spyOn(stores.projectApi, 'getReceipt').mockResolvedValue(receipt);
  render(<WorkspaceStoresContext.Provider value={stores}>
    <ReceiptInspector receiptId={receipt.receiptId} />
  </WorkspaceStoresContext.Provider>);
  await screen.findByTestId('receipt-outputs');
}

it.each([
  [{ filename: 'entities.ftm.zip' }, 'entities.ftm.zip'],
  [{ project_path: 'exports/tables/column_tables.zip' }, 'column_tables.zip'],
  [{}, 'export'],
])('downloads a committed project export using its receipt project and filename (%j)', async (names, filename) => {
  const ref = { kind: 'export_project_file', blob_hash: digest, ...names };
  await inspect(ref);
  const link = screen.getByRole('link', { name: `Download ${filename}` });
  expect(link).toHaveAttribute('href', `/api/projects/source%2Fproject%20%3F/blobs/${digest}`);
  expect(link).toHaveAttribute('download', filename);
  expect(screen.getByTestId('receipt-outputs')).toHaveTextContent(JSON.stringify(ref));
});

it.each(['', 'abc', `sha256:${digest}`, 'A'.repeat(64), 'g'.repeat(64), `${digest}/other`, `${digest}\n`, null, 123])(
  'does not link an invalid blob digest (%j)', async (blob_hash) => {
    await inspect({ kind: 'export_project_file', blob_hash, filename: 'export.zip' });
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  },
);

it.each(['export_artifact', 'external_export', 'source', undefined])(
  'does not infer downloads from another ref kind (%j)', async (kind) => {
    await inspect({ kind, blob_hash: digest, url: 'https://example.invalid/download' }, {
      value: { blob_hash: digest, filename: 'not-a-receipt.zip' },
    });
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  },
);

it('keeps an already committed export downloadable after a later action failure', async () => {
  await inspect({ kind: 'export_project_file', blob_hash: digest, filename: 'export.zip' }, {
    status: 'failed', errors: [{ code: 'later_failure', message: 'Failed after export.' }],
  });
  expect(screen.getByRole('link', { name: 'Download export.zip' })).toHaveAttribute('download', 'export.zip');
});
