import { describe, expect, it } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { ColumnDef } from '../../src/api/types';
import { columnOptionsForParam } from '../../src/components/action-panel/formControlHelpers';
import { servedActionCatalog } from '../support/servedActionCatalog';

const templates = actionTemplatesFromCatalog(servedActionCatalog());

describe('Judge answer-to-grade picker', () => {
  it('offers only AI-generated columns', () => {
    const judge = templates.find((action) => action.kind === 'map.judge');
    const param = judge?.params?.find((candidate) => candidate.name === 'judged_column');
    expect(param?.aiGeneratedOnly).toBe(true);

    const columns: ColumnDef[] = [
      { id: '1', name: 'source', type: 'text' },
      {
        id: '2',
        name: 'answer',
        type: 'text',
        ai: { actionName: 'Extract', prompt: '', model: 'test', costSoFar: 0, versions: [] },
      },
    ];
    expect(columnOptionsForParam(param!, columns).map((column) => column.name)).toEqual([
      'answer',
    ]);
  });
});
