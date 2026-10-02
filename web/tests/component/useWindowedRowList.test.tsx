// @vitest-environment jsdom

import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useWindowedRowList } from '../../src/workbench/useWindowedRowList';

function rows(count: number) {
  return Array.from({ length: count }, (_, index) => ({ id: String(index + 1) }));
}

describe('useWindowedRowList', () => {
  it('reconciles its window after the browser clamps a shortened list scroll offset', () => {
    const initialRows = rows(300);
    const { result, rerender } = renderHook(
      ({ values }) => useWindowedRowList({ rows: values, activeId: null, onSelect: vi.fn() }),
      { initialProps: { values: initialRows } },
    );
    const viewport = document.createElement('div');
    Object.defineProperty(viewport, 'clientHeight', { value: 600 });
    Object.defineProperty(result.current.listBodyRef, 'current', { value: viewport, configurable: true });

    act(() => {
      viewport.scrollTop = 12_000;
      result.current.onListScroll();
    });
    expect(result.current.startIndex).toBeGreaterThan(0);

    // Chromium clamps this property when the scroller height shrinks, but
    // does not emit the event that normally updates the hook.
    viewport.scrollTop = 0;
    rerender({ values: initialRows.slice(0, 1) });

    expect(result.current.startIndex).toBe(0);
    expect(result.current.windowRows).toEqual([{ id: '1' }]);
  });
});
