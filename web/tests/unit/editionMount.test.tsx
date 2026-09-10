// @vitest-environment jsdom

import { createElement } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { PREFERENCES_KEY } from '../../src/settings/preferences';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';

const rootRender = vi.hoisted(() => vi.fn());

vi.mock('react-dom/client', () => ({
  createRoot: vi.fn(() => ({ render: rootRender })),
}));

afterEach(() => {
  window.localStorage.clear();
  document.body.replaceChildren();
  delete document.documentElement.dataset.frisketTheme;
  rootRender.mockReset();
  vi.resetModules();
});

it('applies the stored theme before rendering any edition root', async () => {
  document.body.innerHTML = '<div id="root"></div>';
  window.localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ theme: 'dark' }));
  rootRender.mockImplementationOnce(() => {
    expect(document.documentElement.dataset.frisketTheme).toBe('dark');
  });

  const { mountEdition } = await import('../../src/entries/mount');
  mountEdition(LOCAL_EDITION_MODULE, createElement('main'));

  expect(rootRender).toHaveBeenCalledOnce();
  expect(document.documentElement.dataset.frisketTheme).toBe('dark');
});
