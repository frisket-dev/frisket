import type { PdfPacketMatchResult } from '../../../api/pdfPacketSplits';

export function packetFindQueues(
  matches: Pick<PdfPacketMatchResult, 'suggested_pages' | 'unsure_pages'> | null,
  confirmed: readonly number[],
  rejected: readonly number[],
): { unsure: number[]; suggested: number[] } {
  const excluded = new Set([...confirmed, ...rejected]);
  const clean = (pages: readonly number[] | undefined) => [...new Set(pages ?? [])]
    .filter((page) => !excluded.has(page))
    .sort((left, right) => left - right);
  return {
    unsure: clean(matches?.unsure_pages),
    suggested: clean(matches?.suggested_pages),
  };
}
