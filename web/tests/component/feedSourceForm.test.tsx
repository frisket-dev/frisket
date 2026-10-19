// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FeedSourceForm } from '../../src/components/importWorkspace/FeedSourceForm';

const api = vi.hoisted(() => ({ createSource: vi.fn(), fetchSource: vi.fn() }));
vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi: api, chromePreferences: { projectId: 'feed-test' } }),
}));
vi.mock('../../src/telemetry/productTelemetry', async (original) => ({
  ...await original<typeof import('../../src/telemetry/productTelemetry')>(),
  sendProductTelemetry: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
  api.createSource.mockResolvedValue({ id: 7, name: 'My feed' });
  api.fetchSource.mockResolvedValue({ sheetId: 42 });
});
afterEach(cleanup);

function setup() {
  const props = { onCreated: vi.fn(), onImported: vi.fn(), onError: vi.fn(), onCancel: vi.fn() };
  render(<FeedSourceForm {...props} />);
  return props;
}

describe('feed setup independent of the import workspace', () => {
  it.each([
    ['rss', 'https://example.test/feed.xml', { merge_strategy: 'append-new' }],
    ['youtube_playlist', 'https://www.youtube.com/playlist?list=PL123', { playlist_url: 'https://www.youtube.com/playlist?list=PL123' }],
    ['youtube_channel', '@frisket', { channel_url: '@frisket' }],
    ['api_list_dicts', 'https://example.test/api/items', { method: 'GET', list_path: '/', schema_policy: 'additive' }],
    ['courtlistener_docket', '123', { docket_id: 123, include_parties: true, include_documents: true }],
  ])('creates %s without automatically polling it', async (kind, url, config) => {
    const props = setup();
    fireEvent.change(screen.getByTestId('source-url'), { target: { value: url } });
    expect(screen.getByTestId('source-kind')).toHaveValue(kind);
    fireEvent.click(screen.getByTestId('source-create'));
    await screen.findByTestId('feed-populate-prompt');
    expect(api.createSource).toHaveBeenCalledWith(expect.objectContaining({
      kind, schedule: null, enabled: true, config: expect.objectContaining(config),
    }));
    expect(api.fetchSource).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('feed-populate-dismiss'));
    expect(props.onCreated).toHaveBeenCalledOnce();
    expect(props.onImported).not.toHaveBeenCalled();
  });

  it('keeps an explicit type selection and validates API inputs before submission', () => {
    setup();
    fireEvent.change(screen.getByTestId('source-kind'), { target: { value: 'api_list_dicts' } });
    fireEvent.change(screen.getByTestId('source-url'), { target: { value: 'http://example.test/feed.xml' } });
    expect(screen.getByTestId('source-kind')).toHaveValue('api_list_dicts');
    expect(screen.getByRole('alert')).toHaveTextContent('HTTPS');
    expect(screen.getByTestId('source-create')).toBeDisabled();
    fireEvent.change(screen.getByTestId('source-url'), { target: { value: 'https://example.test/feed.xml' } });
    fireEvent.change(screen.getByTestId('source-api-list-path'), { target: { value: 'items' } });
    expect(screen.getByRole('alert')).toHaveTextContent('must start with /');
    expect(api.createSource).not.toHaveBeenCalled();
  });

  it('offers retry after a failed populate, then opens the returned sheet', async () => {
    const props = setup();
    api.fetchSource.mockRejectedValueOnce(new Error('temporary failure'));
    fireEvent.change(screen.getByTestId('source-url'), { target: { value: 'https://example.test/feed.xml' } });
    fireEvent.click(screen.getByTestId('source-create'));
    await screen.findByTestId('feed-populate-prompt');
    fireEvent.click(screen.getByTestId('feed-populate-run'));
    expect(await screen.findByRole('alert')).toHaveTextContent('temporary failure');
    expect(props.onCreated).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('feed-populate-run'));
    await waitFor(() => expect(props.onImported).toHaveBeenCalledWith(42));
    expect(api.fetchSource).toHaveBeenLastCalledWith(7);
    expect(props.onCreated).toHaveBeenCalledOnce();
  });
});
