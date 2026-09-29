import { describe, expect, it } from 'vitest';
import { GridCellKind, type GridCell } from '@glideapps/glide-data-grid';
import { withWrapSafeDisplayData } from '../../src/grid/cells';

describe('withWrapSafeDisplayData', () => {
  it('protects empty wrap segments without changing raw cell data', () => {
    const raw = 'Opening paragraph.\n\nSecond paragraph.';
    const cell: GridCell = {
      kind: GridCellKind.Text,
      data: raw,
      displayData: raw,
      allowOverlay: false,
      allowWrapping: true,
    };

    const safe = withWrapSafeDisplayData(cell);

    expect(safe).not.toBe(cell);
    expect(safe).toMatchObject({
      data: raw,
      displayData: 'Opening paragraph.\n \nSecond paragraph.',
    });
    expect(withWrapSafeDisplayData(safe)).toBe(safe);
  });

  it('retains cell identity when no empty wrapping segment needs protection', () => {
    const cell: GridCell = {
      kind: GridCellKind.Text,
      data: 'One paragraph',
      displayData: 'One paragraph',
      allowOverlay: false,
      allowWrapping: true,
    };

    expect(withWrapSafeDisplayData(cell)).toBe(cell);
  });
});
