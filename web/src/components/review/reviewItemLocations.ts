import type { ReviewBundleField } from '../../api/types';
import type { ReviewCitationSource } from './reviewCitationSources';

export interface ReviewItemSelection { fieldId: string; index: number; }
export interface ReviewItemLocation extends ReviewItemSelection { sourceId: string; spanId: string; }

/** Result indices come from the evidence writer, never from span ordering.
 * Older NER records can recover a location from their exact quote and offsets. */
export function reviewItemLocations(sources: readonly ReviewCitationSource[], fields: readonly ReviewBundleField[]): ReviewItemLocation[] {
  const locations: ReviewItemLocation[] = [];
  for (const field of fields) {
    const value = parsed(field.value);
    if (!Array.isArray(value)) continue;
    for (const source of sources) {
      for (const member of source.members.filter((candidate) => candidate.fieldId === field.id)) {
        for (const span of member.spans) {
          const raw = record(span.raw);
          const index = member.itemIndex ?? raw?.item_index;
          if (typeof index === 'number' && Number.isInteger(index) && index >= 0 && index < value.length) {
            locations.push({ fieldId: field.id, index, sourceId: source.id, spanId: span.stable_id });
            continue;
          }
          if (field.semanticType !== 'entity_mentions') continue;
          const selector = record(span.selector);
          value.forEach((item, itemIndex) => {
            const entity = record(item);
            if (entity && typeof entity.start === 'number' && typeof entity.end === 'number'
              && entity.start === selector?.char_start && entity.end === selector?.char_end
              && typeof entity.text === 'string' && entity.text === span.quote) {
              locations.push({ fieldId: field.id, index: itemIndex, sourceId: source.id, spanId: span.stable_id });
            }
          });
        }
      }
    }
  }
  return locations;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function parsed(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return value; }
}
