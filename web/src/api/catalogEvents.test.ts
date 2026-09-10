import { afterEach, describe, expect, it, vi } from 'vitest';

import { emitActionCatalogInvalidated, onActionCatalogInvalidated } from './catalogEvents';

describe('action-catalog invalidation bus', () => {
  const unsubs: Array<() => void> = [];
  afterEach(() => {
    while (unsubs.length) unsubs.pop()?.();
  });

  it('notifies every live subscriber on emit', () => {
    const a = vi.fn();
    const b = vi.fn();
    unsubs.push(onActionCatalogInvalidated(a), onActionCatalogInvalidated(b));

    emitActionCatalogInvalidated();

    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('stops notifying after unsubscribe', () => {
    const listener = vi.fn();
    const unsub = onActionCatalogInvalidated(listener);
    unsub();

    emitActionCatalogInvalidated();

    expect(listener).not.toHaveBeenCalled();
  });

  it('re-runs on repeated invalidations (each secret change refetches)', () => {
    const listener = vi.fn();
    unsubs.push(onActionCatalogInvalidated(listener));

    emitActionCatalogInvalidated();
    emitActionCatalogInvalidated();

    expect(listener).toHaveBeenCalledTimes(2);
  });
});
