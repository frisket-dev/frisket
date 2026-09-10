import { describe, expect, it } from 'vitest';
import {
  consumeGridCellReveal,
  getPendingGridCellReveal,
  requestGridCellReveal,
  subscribeGridCellReveal,
} from '../../src/grid/gridCellReveal';

const reveal = (rowId: string) => ({
  projectId: 'project-1',
  sheetId: 'sheet-2',
  rowId,
  columnId: 'column-3',
  columnName: 'story',
});

describe('grid cell reveal handoff', () => {
  it('survives a sheet unmount until the destination grid consumes it', () => {
    const observed: Array<string | null> = [];
    const unsubscribe = subscribeGridCellReveal(() => {
      observed.push(getPendingGridCellReveal()?.rowId ?? null);
    });

    const request = requestGridCellReveal(reveal('row-4'));
    expect(getPendingGridCellReveal()).toEqual(request);
    consumeGridCellReveal(request.requestId);

    expect(getPendingGridCellReveal()).toBeNull();
    expect(observed).toEqual(['row-4', null]);
    unsubscribe();
  });

  it('does not let an obsolete consumer clear a newer search hit', () => {
    const oldRequest = requestGridCellReveal(reveal('row-old'));
    const newRequest = requestGridCellReveal(reveal('row-new'));

    consumeGridCellReveal(oldRequest.requestId);
    expect(getPendingGridCellReveal()).toEqual(newRequest);

    consumeGridCellReveal(newRequest.requestId);
    expect(getPendingGridCellReveal()).toBeNull();
  });
});
