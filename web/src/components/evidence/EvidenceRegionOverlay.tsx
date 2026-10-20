import './EvidenceReading.css';
import type { EvidenceRegion } from '../../api/open';
import { PageRegion } from '../PageRegion';
import { normalizeRegion } from '../regionGeometry';
import { finiteNumber, jsonRecord } from './evidenceSourceModel';

export function RegionOverlay({ region, emphasized, coordinateSpace = 'page_normalized' }: {
  region: EvidenceRegion; emphasized: boolean; coordinateSpace?: 'page_normalized' | 'frame_normalized';
}) {
  const normalized = normalizedBBox(region.bbox, coordinateSpace);
  if (!normalized) return null;
  return (
    <PageRegion
      box={normalized}
      className={`evidence-region${emphasized ? ' evidence-region-emphasized' : ''}`}
      data-testid="evidence-region-highlight"
      data-emphasized={emphasized ? 'true' : undefined}
      title={region.snippet ?? region.stable_id}
    />
  );
}

function normalizedBBox(boxes: unknown, coordinateSpace: 'page_normalized' | 'frame_normalized'): {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
} | null {
  if (!Array.isArray(boxes)) return null;
  for (const value of boxes) {
    const box = jsonRecord(value);
    if (!box) continue;
    if (box.space !== coordinateSpace) continue;
    const x0 = finiteNumber(box.x0);
    const y0 = finiteNumber(box.y0);
    const x1 = finiteNumber(box.x1);
    const y1 = finiteNumber(box.y1);
    if (x0 === null || y0 === null || x1 === null || y1 === null) continue;
    return normalizeRegion({ x: x0, y: y0 }, { x: x1, y: y1 });
  }
  return null;
}
