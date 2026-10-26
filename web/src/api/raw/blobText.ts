import { ApiError } from '../contractErrors';

/** Named reads of original text blobs; bounded reading and explicit full copy. */
export interface BlobTextWindow {
  coordinate: 'bytes';
  section_start: number;
  total_bytes: number;
  range: { start: number; end: number };
  text: string;
  previous_start: number | null;
  next_start: number | null;
}

export const BLOB_TEXT_SECTION_BYTES = 16_384;

export async function readDocumentTextWindow(
  url: string,
  { start = 0, signal }: { start?: number; signal: AbortSignal },
): Promise<BlobTextWindow> {
  const sectionStart = Math.floor(Math.max(0, start) / BLOB_TEXT_SECTION_BYTES) * BLOB_TEXT_SECTION_BYTES;
  const from = Math.max(0, sectionStart - 3);
  const to = sectionStart + BLOB_TEXT_SECTION_BYTES + 2;
  const response = await fetch(url, { signal, headers: { Range: `bytes=${from}-${to}` } });
  const contentRange = response.headers.get('Content-Range');
  if (response.status === 416) {
    await response.body?.cancel();
    const total = Number(/^bytes \*\/(\d+)$/.exec(contentRange ?? '')?.[1]);
    if (total === 0) return { coordinate: 'bytes', section_start: 0, total_bytes: 0, range: { start: 0, end: 0 },
      text: '', previous_start: null, next_start: null };
    if (Number.isSafeInteger(total) && total > 0 && sectionStart >= total) {
      return readDocumentTextWindow(url, { start: Math.floor((total - 1) / BLOB_TEXT_SECTION_BYTES) * BLOB_TEXT_SECTION_BYTES, signal });
    }
    throw new Error('Could not load this section.');
  }
  if (response.status !== 206) {
    await response.body?.cancel();
    if (!response.ok) throw new ApiError(response.status, `Could not load this file (${response.status}).`);
    throw new Error('This server cannot show text sections. Download the file to read it.');
  }
  const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(contentRange ?? '');
  const [rangeStart, rangeEnd, total] = match ? match.slice(1).map(Number) : [];
  if (!match || ![rangeStart, rangeEnd, total].every(Number.isSafeInteger)
    || rangeStart !== from || rangeEnd < rangeStart || rangeEnd > to || total <= rangeEnd) {
    await response.body?.cancel();
    throw new Error('Could not load this section.');
  }
  if (sectionStart >= total) {
    await response.body?.cancel();
    return readDocumentTextWindow(url, { start: Math.floor((total - 1) / BLOB_TEXT_SECTION_BYTES) * BLOB_TEXT_SECTION_BYTES, signal });
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.length !== rangeEnd - rangeStart + 1) throw new Error('Could not load this section.');
  // Adjacent sections share the scalar crossing their boundary exactly once.
  // At most three continuation bytes surround either side of a UTF-8 scalar.
  const continuation = (index: number) => (bytes[index] & 0xc0) === 0x80;
  let first = sectionStart - rangeStart;
  while (first > 0 && continuation(first)) first -= 1;
  let last = Math.min(sectionStart + BLOB_TEXT_SECTION_BYTES - rangeStart, bytes.length);
  while (last > first && last < bytes.length && continuation(last)) last -= 1;
  const text = new TextDecoder('utf-8', { ignoreBOM: sectionStart !== 0 }).decode(bytes.subarray(first, last));
  return { coordinate: 'bytes', section_start: sectionStart, total_bytes: total,
    range: { start: rangeStart + first, end: rangeStart + last }, text,
    previous_start: sectionStart > 0 ? sectionStart - BLOB_TEXT_SECTION_BYTES : null,
    next_start: sectionStart + BLOB_TEXT_SECTION_BYTES < total ? sectionStart + BLOB_TEXT_SECTION_BYTES : null };
}

/** Legacy evidence without persisted text coordinates keeps its quote fallback. */
export async function readEvidenceTextResource(url: string, signal: AbortSignal): Promise<string> {
  return (await readDocumentTextWindow(url, { signal })).text;
}

/** Full file text is fetched only for an explicit Copy full file action. */
export async function readDocumentTextResource(
  url: string,
  { signal }: { signal: AbortSignal },
): Promise<string> {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new ApiError(response.status, `Could not load this file (${response.status}).`);
  return response.text();
}
