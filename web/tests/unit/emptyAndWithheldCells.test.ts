// Grid cell markers for genuinely-empty and withheld values (the live incident:
// the model emitted the string "null", 4 chars indistinguishable from an empty
// cell). A real empty scalar draws a dim em-dash; a withheld value (citation
// grounding or a required field the model returned null for) draws its own
// subdued badge instead of the red error branch.
import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it } from 'vitest';

import { buildCell } from '../../src/grid/cells';
import type { GridCellPalette } from '../../src/grid/gridTheme';
import { columnDef, row } from '../support/domainFixtures';

const injectedPalette: GridCellPalette = {
  error: { textDark: '#c13238', textLight: '#c13238' },
  empty: { textDark: '#73818f', textLight: '#73818f' },
  pending: { background: '#2d2752', highlight: '#b9a7ff' },
};

describe('buildCell empty / withheld markers', () => {
  it('uses an injected palette for error, empty, and pending synthetic cells', () => {
    const col = columnDef({ id: 'name', name: 'name', type: 'text' });

    const error = buildCell(
      col,
      row({ name: null }, { cellErrors: { name: 'model failed' } }),
      { gridCellPalette: injectedPalette },
    );
    expect((error as { themeOverride?: unknown }).themeOverride).toEqual(injectedPalette.error);

    const empty = buildCell(col, row({ name: null }), { gridCellPalette: injectedPalette });
    expect((empty as { themeOverride?: unknown }).themeOverride).toEqual(injectedPalette.empty);

    const pending = buildCell(
      col,
      row({ name: null }),
      { pending: true, gridCellPalette: injectedPalette },
    );
    expect(pending.kind).toBe(GridCellKind.Custom);
    expect((pending as { data?: unknown }).data).toEqual({
      kind: 'frisket-pending',
      palette: injectedPalette.pending,
    });
  });

  it('renders a dim em-dash for a genuinely empty text cell', () => {
    const col = columnDef({ id: 'name', name: 'name', type: 'text' });
    const cell = buildCell(col, row({ name: null }));
    expect(cell.kind).toBe(GridCellKind.Text);
    // The visible marker is the em-dash, muted to the design-system token...
    expect((cell as { displayData?: string }).displayData).toBe('—');
    expect((cell as { themeOverride?: { textDark?: string } }).themeOverride?.textDark).toBe(
      '#8a8375',
    );
    // ...but copy data stays empty (never copy an em-dash out of an empty cell).
    expect((cell as { data?: string }).data).toBe('');
  });

  it('preserves the previous pending colors for a standalone builder', () => {
    const col = columnDef({ id: 'name', name: 'name', type: 'text' });
    const cell = buildCell(col, row({ name: null }), { pending: true });
    expect((cell as { data?: unknown }).data).toEqual({
      kind: 'frisket-pending',
      palette: { background: '#ece4f9', highlight: '#7c5ce6' },
    });
  });

  it('renders the em-dash for an empty string too', () => {
    const col = columnDef({ id: 'name', name: 'name', type: 'text' });
    const cell = buildCell(col, row({ name: '' }));
    expect((cell as { displayData?: string }).displayData).toBe('—');
  });

  it('leaves a populated cell untouched', () => {
    const col = columnDef({ id: 'name', name: 'name', type: 'text' });
    const cell = buildCell(col, row({ name: 'Ada Lovelace' }));
    expect((cell as { displayData?: string }).displayData).not.toBe('—');
  });

  it('preserves an invalid typed value as an editable red cell', () => {
    const col = columnDef({ id: 'amount', name: 'amount', type: 'number' });
    const cell = buildCell(
      col,
      row({ amount: 'N/A' }, { invalidCells: { amount: true } }),
      { editable: true, gridCellPalette: injectedPalette },
    );
    expect(cell.kind).toBe(GridCellKind.Text);
    expect((cell as { displayData?: string }).displayData).toBe('N/A');
    expect((cell as { themeOverride?: unknown }).themeOverride).toEqual(
      injectedPalette.error,
    );
    expect(cell.allowOverlay).toBe(true);
  });

  it('does not em-dash a boolean cell (it has its own empty affordance)', () => {
    const col = columnDef({ id: 'flag', name: 'flag', type: 'boolean' });
    const cell = buildCell(col, row({ flag: null }));
    expect(cell.kind).toBe(GridCellKind.Boolean);
  });

  it('renders a withheld badge for withheld_unverified, not the red error', () => {
    const col = columnDef({ id: 'name', name: 'name', type: 'text' });
    const cell = buildCell(
      col,
      row(
        { name: null },
        {
          cellOutcomes: { name: 'withheld_unverified' },
          // withhold also records a reason in the error column; it must NOT
          // surface as a red failure -- the withheld badge wins.
          cellErrors: { name: 'map.extract required field was null or missing' },
        },
      ),
    );
    expect(cell.kind).toBe(GridCellKind.Bubble);
    expect((cell as { data?: string[] }).data).toEqual(['withheld']);
  });
});
