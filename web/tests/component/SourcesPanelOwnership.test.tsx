// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { SourceDetail, SourceInfo } from '../../src/api/open';
import type { SourceApiPort } from '../../src/api/ports';
import { SourcesPanel } from '../../src/components/SourcesPanel';

afterEach(cleanup);

function source(overrides: Partial<SourceInfo> = {}): SourceInfo {
  return {
    id: 7, name: 'Records', kind: 'rss', url: 'https://example.test/feed',
    config: { merge_strategy: 'append-new' }, sheetId: 2, schedule: '@hourly',
    enabled: true, lastCheckedAt: null, lastStatus: 'ok', newRowsTotal: 3,
    ...overrides,
  };
}

function setup(item = source()) {
  const detail: SourceDetail = {
    ...item,
    runs: [{ id: 8, status: 'error', newRows: 1, changedRows: 2, skippedRows: 3,
      revisions: 4, warningCount: 1, durationMs: 1000, costMicro: 0,
      receiptId: null, error: 'Redacted poll failure', startedAt: '2026-10-19T12:00:00Z', finishedAt: null }],
    runsPage: { schemaVersion: 'frisket.source_runs_page.v1', order: 'desc', offset: 0,
      limit: 20, total: 23, hasMore: true, nextOffset: 20, latestRun: null, latestRunLoaded: false },
  };
  const api = {
    listSources: vi.fn().mockResolvedValue([item]),
    getSource: vi.fn().mockResolvedValue(detail),
    createSource: vi.fn(), updateSource: vi.fn().mockResolvedValue(item),
    deleteSource: vi.fn().mockResolvedValue(undefined),
    getSourceHealth: vi.fn(), fetchSource: vi.fn().mockResolvedValue({ sheetId: 2 }),
  } satisfies SourceApiPort;
  const onPolled = vi.fn();
  render(<SourcesPanel sourceApi={api} onPolled={onPolled}
    sourceKindFormFrame={({ sourceKind, children }) => <div data-testid={`kind-frame-${sourceKind}`}>{children}</div>} />);
  return { api, onPolled };
}

it('redacts RSS credentials and query strings, and cancels edits without saving', async () => {
  const { api } = setup(source({ url: 'https://user:password@example.test/feed?token=secret#private' }));
  await screen.findByTestId('source-item-7');
  expect(screen.getByText('https://example.test/feed')).toHaveAttribute('title', 'https://example.test/feed');
  expect(document.body).not.toHaveTextContent('secret');
  expect(document.body).not.toHaveTextContent('password');
  fireEvent.click(screen.getByTestId('source-edit-7'));
  expect(screen.getByTestId('kind-frame-rss')).toBeInTheDocument();
  expect(screen.getByTestId('source-url')).toHaveValue('');
  expect(screen.getByTestId('source-create')).toBeDisabled();
  fireEvent.change(screen.getByTestId('source-name'), { target: { value: 'Draft name' } });
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByTestId('source-form')).not.toBeInTheDocument();
  expect(api.updateSource).not.toHaveBeenCalled();
  expect(api.createSource).not.toHaveBeenCalled();
  fireEvent.click(screen.getByTestId('source-edit-7'));
  expect(screen.getByTestId('source-name')).toHaveValue('Records');
});

it('validates API edits before saving the versioned source config', async () => {
  const { api } = setup(source({ kind: 'api_list_dicts', config: { list_path: '/items', max_items: 12 } }));
  fireEvent.click(await screen.findByTestId('source-edit-7'));
  fireEvent.change(screen.getByTestId('source-url'), { target: { value: 'http://example.test/api' } });
  fireEvent.submit(screen.getByTestId('source-form'));
  expect(screen.getByTestId('sources-error')).toHaveTextContent('HTTPS');
  expect(api.updateSource).not.toHaveBeenCalled();
  fireEvent.change(screen.getByTestId('source-url'), { target: { value: 'https://example.test/api' } });
  fireEvent.click(screen.getByTestId('source-create'));
  await waitFor(() => expect(api.updateSource).toHaveBeenCalledWith(7, expect.objectContaining({
    kind: 'api_list_dicts', url: 'https://example.test/api', schedule: '@hourly',
    config: { schema_version: 'frisket.source.api_list_dicts.v1', method: 'GET',
      url: 'https://example.test/api', list_path: '/items', schema_policy: 'additive', max_items: 12 },
  })));
  expect(api.fetchSource).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.queryByTestId('source-form')).not.toBeInTheDocument());
});

it('requires two clicks to delete and removes the confirmed source', async () => {
  const { api } = setup();
  fireEvent.click(await screen.findByTestId('source-delete-7'));
  expect(api.deleteSource).not.toHaveBeenCalled();
  expect(screen.getByTestId('source-delete-7')).toHaveAccessibleName('Confirm delete Records');
  fireEvent.click(screen.getByTestId('source-delete-7'));
  await screen.findByTestId('sources-empty');
  expect(api.deleteSource).toHaveBeenCalledExactlyOnceWith(7);
});

it.each(['source-api-list-path', 'source-api-item-id-path', 'source-api-updated-at-path'])('rejects invalid pointers from editor field %s', async (field) => {
  const { api } = setup(source({ kind: 'api_list_dicts' }));
  fireEvent.click(await screen.findByTestId('source-edit-7'));
  fireEvent.change(screen.getByTestId(field), { target: { value: '/bad~2escape' } });
  fireEvent.submit(screen.getByTestId('source-form'));
  expect(screen.getByTestId('sources-error')).toHaveTextContent('invalid ~ escape');
  expect(api.updateSource).not.toHaveBeenCalled();
});

it('preserves the max-entry config when editing a numeric docket', async () => {
  const { api } = setup(source({ kind: 'courtlistener_docket', url: null,
    config: { docket_id: 123, max_entries_per_poll: 4 } }));
  fireEvent.click(await screen.findByTestId('source-edit-7'));
  expect(screen.getByTestId('source-court-max-entries')).toHaveValue('4');
  fireEvent.change(screen.getByTestId('source-court-max-entries'), { target: { value: '6' } });
  fireEvent.click(screen.getByTestId('source-create'));
  await waitFor(() => expect(api.updateSource).toHaveBeenCalledWith(7, expect.objectContaining({
    url: null, config: expect.objectContaining({ docket_id: 123, max_entries_per_poll: 6 }),
  })));
});

it('refreshes run history after a failed poll and retains the unloaded-page notice', async () => {
  const { api, onPolled } = setup();
  api.fetchSource.mockRejectedValueOnce(new Error('Connection refused'));
  fireEvent.click(await screen.findByTestId('source-fetch-7'));
  expect(await screen.findByTestId('source-runs-page-note-7')).toHaveTextContent('23 total polls / older polls not loaded');
  expect(screen.getByTestId('source-runs-7')).toHaveTextContent('1 new / 2 changed / 4 revised / 3 skipped');
  expect(screen.getByTestId('source-runs-7')).toHaveTextContent('Redacted poll failure');
  expect(screen.getByTestId('sources-error')).toHaveTextContent('Poll failed: Connection refused');
  expect(api.getSource).toHaveBeenCalledWith(7, 0, 20);
  expect(onPolled).not.toHaveBeenCalled();
});
