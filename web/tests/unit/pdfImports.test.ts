// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest';

import { createPdfImportsApi, PdfImportResponseError } from '../../src/api/pdfImports';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('PDF imports API', () => {
  it('uses generated route metadata for create and preserves the typed request body', async () => {
    const fetch = vi.fn(async () => jsonResponse({ draft_id: 'draft-1' }, 201));
    const api = createPdfImportsApi();

    await api.create('project / one', 2, 17, 'Folder', { fetch });

    expect(fetch).toHaveBeenCalledTimes(1);
    const [path, init] = fetch.mock.calls[0];
    expect(path).toBe('/api/projects/project%20%2F%20one/import/pdf-imports');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_files: 2,
      expected_bytes: 17,
      source_label: 'Folder',
    });
  });

  it('keeps multipart file, occurrence, and logical-path fields index aligned', async () => {
    const fetch = vi.fn(async () => jsonResponse({ draft_id: 'draft-1' }));
    const api = createPdfImportsApi();
    const one = new File(['one'], 'one.pdf', { type: 'application/pdf' });
    const two = new File(['two'], 'two.pdf', { type: 'application/pdf' });

    await api.upload('project-1', 'draft-1', [
      { fileId: 'occurrence-1', file: one, logicalPath: 'Folder/one.pdf' },
      { fileId: 'occurrence-2', file: two, logicalPath: 'Folder/two.pdf' },
    ], 'batch-1', { fetch });

    const [path, init] = fetch.mock.calls[0];
    expect(path).toBe('/api/projects/project-1/import/pdf-imports/draft-1/uploads');
    const body = init?.body as FormData;
    expect(body.getAll('files')).toEqual([one, two]);
    expect(body.getAll('file_ids')).toEqual(['occurrence-1', 'occurrence-2']);
    expect(body.getAll('logical_paths')).toEqual(['Folder/one.pdf', 'Folder/two.pdf']);
    expect(body.get('batch_id')).toBe('batch-1');
    expect(new Headers(init?.headers).has('Content-Type')).toBe(false);
  });

  it('uses the generated staged-analysis route without inventing a request body', async () => {
    const fetch = vi.fn(async () => jsonResponse({ draft_id: 'draft-1', revision: 4 }));
    const api = createPdfImportsApi();

    await api.analyze('project / one', 'draft / one', { fetch });

    expect(fetch).toHaveBeenCalledTimes(1);
    const [path, init] = fetch.mock.calls[0];
    expect(path).toBe('/api/projects/project%20%2F%20one/import/pdf-imports/draft%20%2F%20one/analyze');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeUndefined();
  });

  it('sends the exact paid OCR approval hash with the final commit', async () => {
    const fetch = vi.fn(async () => jsonResponse({ draft_id: 'draft-1', status: 'accepted' }, 202));
    const api = createPdfImportsApi();
    const confirmation = 'a'.repeat(64);

    await api.commit('project-1', 'draft-1', {
      expected_revision: 9,
      idempotency_key: 'commit-key-1',
      ocr_confirmation: confirmation,
    }, { fetch });

    const [path, init] = fetch.mock.calls[0];
    expect(path).toBe('/api/projects/project-1/import/pdf-imports/draft-1/commit');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_revision: 9,
      idempotency_key: 'commit-key-1',
      ocr_confirmation: confirmation,
    });
  });

  it('sends the missing-text OCR policy and preserves bounded extension counts', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ draft_id: 'draft-1' }))
      .mockResolvedValueOnce(jsonResponse({
        category: 'other',
        items: [],
        next_cursor: null,
        extension_counts: [{ extension: '.zip', count: 3 }],
        unlisted_extension_count: 2,
      }));
    const api = createPdfImportsApi();

    await api.updateChoices('project-1', 'draft-1', {
      expected_revision: 7,
      destination: { kind: 'new', sheet_name: 'Imported files' },
      ocr_policy: 'missing',
      ocr_selection: { kind: 'engine', engine: 'rapidocr' },
      semantic_search: false,
      semantic_setup_confirmation: null,
    }, { fetch });
    const samples = await api.samples('project-1', 'draft-1', 'other', undefined, { fetch });

    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual(expect.objectContaining({
      ocr_policy: 'missing',
    }));
    expect(samples.extension_counts).toEqual([{ extension: '.zip', count: 3 }]);
    expect(samples.unlisted_extension_count).toBe(2);
  });

  it('preserves the PDF workflow response error used for revision retries', async () => {
    const fetch = vi.fn(async () => jsonResponse({ detail: 'revision changed' }, 409));
    const api = createPdfImportsApi();

    await expect(api.get('project-1', 'draft-1', { fetch })).rejects.toEqual(
      expect.objectContaining<PdfImportResponseError>({
        name: 'PdfImportResponseError',
        status: 409,
        message: 'revision changed',
      }),
    );
  });
});
