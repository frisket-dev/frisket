import type {
  TopicSegmentationCompareCanonicalBoundary,
  TopicSegmentationCompareUnit,
} from '../api/types';

/** Boundary identity is the canonical following-unit ordinal. Point/span is
 * presentation detail and intentionally does not affect equality. */
export function differingBoundaryKeys(
  left: readonly TopicSegmentationCompareCanonicalBoundary[],
  right: readonly TopicSegmentationCompareCanonicalBoundary[],
): number[] {
  const leftKeys = new Set(left.map((boundary) => boundary.key));
  const rightKeys = new Set(right.map((boundary) => boundary.key));
  return [...new Set([...leftKeys, ...rightKeys])]
    .filter((key) => leftKeys.has(key) !== rightKeys.has(key))
    .sort((a, b) => a - b);
}

export function formatTopicUnitTime(unit: TopicSegmentationCompareUnit): string | null {
  if (unit.start_ms === null || unit.end_ms === null) return null;
  const time = (milliseconds: number) => {
    const totalSeconds = milliseconds / 1_000;
    const hours = Math.floor(totalSeconds / 3_600);
    const minutes = Math.floor((totalSeconds % 3_600) / 60);
    const seconds = totalSeconds % 60;
    const short = `${String(minutes).padStart(2, '0')}:${seconds.toFixed(3).padStart(6, '0')}`;
    return hours ? `${String(hours).padStart(2, '0')}:${short}` : short;
  };
  return `${time(unit.start_ms)}–${time(unit.end_ms)}`;
}
