// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { topLayerPortalRoot } from '../../src/topLayerPortal';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('topLayerPortalRoot', () => {
  it('uses the document body when no modal dialog is active', () => {
    expect(topLayerPortalRoot()).toBe(document.body);
  });

  it('uses the last active modal so portaled controls are not inert', () => {
    const first = document.createElement('dialog');
    const last = document.createElement('dialog');
    vi.spyOn(document, 'querySelectorAll').mockReturnValue({
      0: first,
      1: last,
      length: 2,
      item: (index: number) => [first, last][index] ?? null,
    } as unknown as NodeListOf<HTMLDialogElement>);

    expect(topLayerPortalRoot()).toBe(last);
  });
});
