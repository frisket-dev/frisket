import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../src/api/contractErrors';
import {
  BLOB_TEXT_SECTION_BYTES,
  readDocumentTextResource,
  readDocumentTextWindow,
} from '../../src/api/raw/blobText';

function serve(text: string) {
  const bytes = new TextEncoder().encode(text);
  const fetch = vi.fn(async (_url: string, options: RequestInit) => {
    const range = /^bytes=(\d+)-(\d+)$/.exec((options.headers as { Range: string }).Range)!;
    const start = Number(range[1]);
    const end = Math.min(Number(range[2]), bytes.length - 1);
    if (start >= bytes.length) return new Response(null, { status: 416,
      headers: { 'Content-Range': `bytes */${bytes.length}` } });
    return new Response(bytes.slice(start, end + 1), { status: 206,
      headers: { 'Content-Range': `bytes ${start}-${end}/${bytes.length}` } });
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}
const signal = () => new AbortController().signal;
afterEach(() => vi.unstubAllGlobals());

describe('original blob text sections', () => {
  it('reassembles multibyte Unicode across sections with bounded transfers and no dropped or duplicated scalars', async () => {
    const text = 'a'.repeat(BLOB_TEXT_SECTION_BYTES - 1) + '🙂漢é\uFEFF'.repeat(5000) + '\u0000tail';
    const fetch = serve(text);
    let start: number | null = 0;
    let assembled = '';
    while (start !== null) {
      const window = await readDocumentTextWindow('/original', { start, signal: signal() });
      assembled += window.text;
      start = window.next_start;
    }
    expect(assembled).toBe(text);
    for (const call of fetch.mock.calls) {
      const range = /^bytes=(\d+)-(\d+)$/.exec((call[1].headers as { Range: string }).Range)!;
      expect(Number(range[2]) - Number(range[1]) + 1).toBeLessThanOrEqual(BLOB_TEXT_SECTION_BYTES + 6);
    }
  });

  it('cancels an ignored Range without decoding or buffering its full body', async () => {
    const cancel = vi.fn().mockResolvedValue(undefined);
    const arrayBuffer = vi.fn();
    const text = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 200, ok: true,
      headers: new Headers(), body: { cancel }, arrayBuffer, text }));
    await expect(readDocumentTextWindow('/original', { signal: signal() })).rejects.toThrow('Download the file');
    expect(cancel).toHaveBeenCalledOnce();
    expect(arrayBuffer).not.toHaveBeenCalled();
    expect(text).not.toHaveBeenCalled();
  });

  it('handles empty files and clamps a section requested beyond EOF', async () => {
    serve('');
    expect(await readDocumentTextWindow('/empty', { signal: signal() })).toMatchObject({
      total_bytes: 0, text: '', next_start: null, previous_start: null,
    });
    const fetch = serve('a'.repeat(BLOB_TEXT_SECTION_BYTES) + 'tail');
    const window = await readDocumentTextWindow('/original', { start: 999999, signal: signal() });
    expect(window).toMatchObject({ section_start: BLOB_TEXT_SECTION_BYTES, text: 'tail', next_start: null });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('keeps a scalar that crosses the final section boundary', async () => {
    const text = 'a'.repeat(BLOB_TEXT_SECTION_BYTES - 1) + '🙂';
    serve(text);
    const first = await readDocumentTextWindow('/original', { signal: signal() });
    const last = await readDocumentTextWindow('/original', { start: first.next_start!, signal: signal() });
    expect(first.text + last.text).toBe(text);
    expect(last.text).toBe('🙂');
    expect(last.previous_start).toBe(0);
  });

  it('passes cancellation to the request and propagates an aborted read', async () => {
    const controller = new AbortController();
    const fetch = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
      options.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    vi.stubGlobal('fetch', fetch);
    const reading = readDocumentTextWindow('/original', { signal: controller.signal });
    controller.abort();
    await expect(reading).rejects.toMatchObject({ name: 'AbortError' });
    expect(fetch).toHaveBeenCalledWith('/original', expect.objectContaining({ signal: controller.signal }));
  });

  it('preserves HTTP status for bounded and full-resource failures', async () => {
    const cancel = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status: 404,
      ok: false,
      headers: new Headers(),
      body: { cancel },
    }));

    await expect(readDocumentTextWindow('/missing', { signal: signal() })).rejects.toEqual(
      expect.objectContaining<ApiError>({ status: 404, message: 'Could not load this file (404).' }),
    );
    await expect(readDocumentTextResource('/missing', { signal: signal() })).rejects.toEqual(
      expect.objectContaining<ApiError>({ status: 404, message: 'Could not load this file (404).' }),
    );
    expect(cancel).toHaveBeenCalledOnce();
  });
});
