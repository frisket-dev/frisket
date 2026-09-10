import { describe, expect, it, vi } from 'vitest';
import { memoizeByArgs } from './memo';

describe('memoizeByArgs', () => {
  it('returns the cached result when every arg is Object.is-equal to the last call', () => {
    const compute = vi.fn((a: number, b: number) => ({ sum: a + b }));
    const memoized = memoizeByArgs(compute);

    const first = memoized(1, 2);
    const second = memoized(1, 2);

    expect(second).toBe(first);
    expect(compute).toHaveBeenCalledTimes(1);
  });

  it('recomputes when any arg differs by reference or value', () => {
    const compute = vi.fn((a: number, b: number) => ({ sum: a + b }));
    const memoized = memoizeByArgs(compute);

    memoized(1, 2);
    const second = memoized(1, 3);

    expect(second).toEqual({ sum: 4 });
    expect(compute).toHaveBeenCalledTimes(2);
  });

  it('treats NaN as equal to itself (Object.is semantics, not ===)', () => {
    const compute = vi.fn((a: number) => ({ value: a }));
    const memoized = memoizeByArgs(compute);

    memoized(NaN);
    memoized(NaN);

    expect(compute).toHaveBeenCalledTimes(1);
  });
});
