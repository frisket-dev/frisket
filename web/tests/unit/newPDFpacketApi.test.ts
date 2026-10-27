import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  applyPdfPacketSheetSplit,
  createPdfPacketSplitFromDraft,
  getPdfPacketSheetScopeToken,
  commitPdfPacketSplit,
  createPdfPacketSplit,
  estimatePdfPacketOcr,
  matchPdfPacketCandidates,
  pdfPacketThumbnailUrl,
  pdfPacketSourceUrl,
  startPdfPacketOcr,
} from '../../src/api/pdfPacketSplits';

const response = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
});

describe('PDF packet split API', () => {
  it('addresses admitted source files without relying on a published blob', () => {
    expect(pdfPacketSourceUrl('project 1', 'split/1', 'row:1/2')).toBe('/api/projects/project%201/import/pdf-packet-splits/split%2F1/source-file?source_key=row%3A1%2F2');
  });

  afterEach(() => vi.unstubAllGlobals());

  it('sends typed draft and view-token requests through the contract routes', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response({ split_id: 's' })).mockResolvedValueOnce(response({ order_hash: 'h' }));
    vi.stubGlobal('fetch', fetchMock);
    await createPdfPacketSplitFromDraft('p', { draft_id: 'd', expected_revision: 3, organization: 'split_documents' });
    await getPdfPacketSheetScopeToken('p', { sheet_id: 1, scope: 'selected', filter_: '{}', sort: null });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/projects/p/import/pdf-packet-splits/from-draft');
    expect(JSON.parse(String(fetchMock.mock.calls[1][1].body))).toEqual({ sheet_id: 1, scope: 'selected', filter_: '{}', sort: null });
  });

  it('requires a durable import job for sheet split application', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ job_id: 7, import_ref: 'i', sheet_id: 3 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(applyPdfPacketSheetSplit('p', 's', { idempotency_key: 'key', destination: { kind: 'replace', sheet_id: 3 } }))
      .resolves.toEqual({ job_id: 7, import_ref: 'i', sheet_id: 3 });
    fetchMock.mockResolvedValueOnce(response({ job_id: 'preview' }));
    await expect(applyPdfPacketSheetSplit('p', 's', { idempotency_key: 'key', destination: { kind: 'replace', sheet_id: 3 } })).rejects.toThrow(/durable import job/);
  });

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
      expected_analysis_revision: 2,
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
    const fetchMock = vi.fn().mockImplementation(async () => response({ job_id: 'job-1' }));
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

  it('projects typed OCR estimates and preserves the cached-only null sentinel', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({
        schema_version: 'frisket.pdf_packet_split.v1',
        engine: 'rapidocr',
        scope: 'sample',
        pages: [17],
        cached_pages: [],
        estimate: {
          rows: 1,
          cost: 0.25,
          cost_source: 'catalog',
          billed_cost: 250_000,
          policy_id: 'hosted.v1',
          requires_confirmation: true,
          promise_set_hash: 'quote-1',
          claims: [{ field: 'cost', display: 'Estimated billed cost $0.25.' }],
        },
      }))
      .mockResolvedValueOnce(response({
        schema_version: 'frisket.pdf_packet_split.v1',
        engine: 'rapidocr',
        scope: 'sample',
        pages: [17],
        cached_pages: [17],
        estimate: null,
      }));
    vi.stubGlobal('fetch', fetchMock);

    const quoted = await estimatePdfPacketOcr('p', 's', {
      engine: 'rapidocr', scope: 'sample', pages: [17],
    });
    const cached = await estimatePdfPacketOcr('p', 's', {
      engine: 'rapidocr', scope: 'sample', pages: [17],
    });

    expect(quoted.estimate).toMatchObject({
      billed_cost: 250_000,
      policy_id: 'hosted.v1',
      promise_set_hash: 'quote-1',
    });
    expect(cached.estimate).toBeNull();
    expect(cached.cached_pages).toEqual([17]);
  });
});
