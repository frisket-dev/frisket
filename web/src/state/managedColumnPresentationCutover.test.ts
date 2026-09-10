import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it } from 'vitest';

import { buildCell } from '../grid/cells';
import { aiMeta, columnDef, row } from '../../tests/support/domainFixtures';

// The managed-column presentation cutover (latest-family metadata replacing
// the legacy scalar value pointer) is pinned BEHAVIORALLY: the wire shape in
// tests/unit/sheetGridHttpContracts.test.ts and
// tests/unit/historyReviewHttpContracts.test.ts, and the grid rendering
// below. Earlier revisions of this file also grepped component source for
// banned/required identifier spellings; those fences were removed per the
// test-fence proportionality ruling (behavioral seams win, and a completed
// cutover's migration fence retires with the cutover).
describe('managed column presentation cutover', () => {
  it('lets landed errors and retained old exact values beat an in-flight pulse', () => {
    const column = columnDef({ type: 'text', ai: aiMeta() });
    const error = buildCell(
      column,
      row({ [column.id]: null }, { cellErrors: { [column.id]: 'exact error' } }),
      { pending: true },
    );
    const retained = buildCell(
      column,
      row({ [column.id]: 'older exact value' }),
      { pending: true },
    );

    expect(error.kind).toBe(GridCellKind.Text);
    expect((error as { displayData?: string }).displayData).toBe('⚠ exact error');
    expect(retained.kind).toBe(GridCellKind.Text);
    expect((retained as { data?: string }).data).toBe('older exact value');
  });
});
