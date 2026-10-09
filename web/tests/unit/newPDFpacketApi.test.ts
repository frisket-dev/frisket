import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  commitPdfPacketSplit,
  createPdfPacketSplit,
  matchPdfPacketCandidates,
  pdfPacketThumbnailUrl,
  startPdfPacketOcr,
} from '../../src/api/pdfPacketSplits';

const response = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
});

describe('PDF packet split API', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('uploads multipart data without overriding its content type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ split_id: 'split-1' }));
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['pdf'], 'packet.pdf', { type: 'application/pdf' });

    await createPdfPacketSplit('project 1', file, 'request-1');

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/projects/project%201/import/pdf-packet-splits');
    expect(init.method).toBe('POST');
    expect(init.headers).toBeUndefined();
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get('file')).toBe(file);
    expect((init.body as FormData).get('request_id')).toBe('request-1');
  });

  it('sends caller-owned labels and the frozen packet-wide candidate controls', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ suggested_pages: [] }));
    vi.stubGlobal('fetch', fetchMock);
    const input = {
      confirmed_starts: [1, 9],
      rejected: [4],
      threshold_pct: 83,
      phrases: [{ id: 'phrase-1', text: 'Dear requester', enabled: true, fuzzy: true }],
    };

    await matchPdfPacketCandidates('p', 'split/1', input);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/projects/p/import/pdf-packet-splits/split%2F1/candidates');
    expect(JSON.parse(String(init.body))).toEqual(input);
  });

  it('keeps OCR confirmation and confirmed-only commit fields explicit', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ job_id: 'job-1' }));
    vi.stubGlobal('fetch', fetchMock);

    await startPdfPacketOcr('p', 's', {
      engine: 'rapidocr',
      scope: 'sample',
      pages: [17],
      confirmation: 'promise-1',
    });
    await commitPdfPacketSplit('p', 's', {
      idempotency_key: 'commit-1',
      confirmed_starts: [1, 17],
      destination: { kind: 'new_sheet', name: 'Packet documents' },
      name_pattern: '{packet} · pp {start}–{end}',
      keep_ocr_text: true,
      remember_options: false,
    });

    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      engine: 'rapidocr', scope: 'sample', pages: [17], confirmation: 'promise-1',
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1].body))).toMatchObject({
      idempotency_key: 'commit-1',
      confirmed_starts: [1, 17],
      destination: { kind: 'new_sheet', name: 'Packet documents' },
    });
    expect(pdfPacketThumbnailUrl('p', 's', 17)).toBe(
      '/api/projects/p/import/pdf-packet-splits/s/pages/17/thumbnail',
    );
  });
});
