import { describe, expect, it } from 'vitest';
import { orderColumnActionsForNextStep } from '../../src/actions/model';
import type { ActionTemplate, ColumnDef } from '../../src/api/types';

const actions: ActionTemplate[] = ['media.ocr', 'media.transcribe', 'media.extract_metadata']
  .map((kind) => ({ kind, name: kind, description: '', defaultPrompt: '', defaultFields: [] }));

describe('transcription next-step ordering', () => {
  it.each(['missing', 'partial'] as const)('prioritizes canonical transcription for %s text', (status) => {
    const column: ColumnDef = { id: '1', name: 'media', type: 'file', transcriptStatus: status };
    const ordered = orderColumnActionsForNextStep(actions, column);
    expect(ordered.map((action) => action.kind)).toEqual([
      'media.transcribe', 'media.ocr', 'media.extract_metadata',
    ]);
    expect(actions[0].kind).toBe('media.ocr');
  });

  it('leaves ordinary columns in their existing order', () => {
    expect(orderColumnActionsForNextStep(actions, { id: '1', name: 'media', type: 'file' }))
      .toBe(actions);
  });
});
