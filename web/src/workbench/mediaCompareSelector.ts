import type { HttpSelectorChoicesQuery } from '../generated/openHttpContracts';
import type { CompareColumn } from './mediaCompareSession';

export type MediaSelectorAction = 'media.ocr' | 'media.transcribe' | 'map.find_topic_sections' | 'map.translate';

/** Translate the comparison's existing option names to canonical action Params. */
export function mediaSelectorQuery(actionId: MediaSelectorAction, column: CompareColumn): HttpSelectorChoicesQuery {
  const params: Record<string, string | boolean | number> = { engine: column.engineId ?? '' };
  const names: Record<string, string> = {
    modelSize: 'model_size', searchablePdf: 'searchable_pdf',
    numSpeakers: 'num_speakers', minSpeakers: 'min_speakers', maxSpeakers: 'max_speakers',
  };
  for (const [name, value] of Object.entries(column.options)) {
    if (typeof value === 'string' || typeof value === 'boolean' || typeof value === 'number') {
      params[names[name] ?? name] = value;
    }
  }
  return {
    schema_version: 'frisket.selector_choices_query.v1',
    subject: { kind: 'action', action_id: actionId, field: 'engine', params },
  };
}
