import { packetFindQueues } from './packetFindQueues';

export interface ReviewSource {
  key: string;
  filename: string;
  page_count?: number;
  page_start: number;
  page_end: number;
  display_start: number;
  blob_hash?: string;
}

export function createReviewSources(sources: readonly ReviewSource[]): readonly ReviewSource[] {
  return [...sources].sort((a, b) => a.display_start - b.display_start);
}

export function pageAddress(sources: readonly ReviewSource[], page: number) {
  const source = sources.find((item) => page >= item.display_start
    && page < item.display_start + item.page_end - item.page_start + 1);
  if (!source) return null;
  const sourcePage = page - source.display_start + 1;
  return { ...source, sourcePage, localPage: source.page_start + sourcePage - 1 };
}

export function reviewedStarts(sources: readonly ReviewSource[], confirmed: readonly number[]): Record<string, number[]> {
  return Object.fromEntries(sources.map((source) => {
    const pages = new Set([source.page_start]);
    for (const page of confirmed) {
      const address = pageAddress(sources, page);
      if (address?.key === source.key) pages.add(address.localPage);
    }
    return [source.key, [...pages].sort((a, b) => a - b)];
  }));
}

export function reviewQueues(
  matches: Parameters<typeof packetFindQueues>[0], fixed: readonly number[], confirmed: readonly number[], rejected: readonly number[],
) {
  return packetFindQueues(matches, [...fixed, ...confirmed], rejected);
}

/** Convert reviewed original-local starts back to the packet's display coordinates. */
export function displayStarts(sources: readonly ReviewSource[], starts: Record<string, readonly number[]>): number[] {
  const result: number[] = [];
  for (const source of sources) {
    const pages = starts[source.key] ?? [];
    for (const localPage of pages) {
      if (localPage < source.page_start || localPage > source.page_end) {
        throw new Error('A reviewed start is outside its source. Reopen the split.');
      }
      result.push(source.display_start + localPage - source.page_start);
    }
  }
  return [...new Set(result)].sort((a, b) => a - b);
}
