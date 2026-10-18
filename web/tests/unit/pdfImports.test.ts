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
