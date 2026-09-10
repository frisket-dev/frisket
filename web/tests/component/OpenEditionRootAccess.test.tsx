// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  defineEditionModule,
  EditionModuleProvider,
} from '../../src/editions/module';
import { OpenEditionRoot } from '../../src/entries/OpenEditionRoot';

vi.mock('../../src/shellIdentity', () => ({
  useShellIdentity: () => ({ identityMode: true, me: null, resolved: true }),
}));
vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: { getDocument: () => undefined, TextLayer: class {} },
}));

const editionId = 'test-public-route-access';
const edition = defineEditionModule({
    descriptor: {
      id: editionId,
      capabilities: {
        configurableNotificationDestinations: false,
        configurableNotificationEmail: false,
        identity: true,
        team: true,
      },
    },
    routes: [
        {
          id: 'public-page',
          path: '/public-page',
          access: 'public',
          component: () => <main data-testid="public-page">Public page</main>,
        },
        {
          id: 'private-page',
          path: '/private-page',
          access: 'authenticated',
          component: () => <main data-testid="private-page">Private page</main>,
        },
    ],
  });

const renderRoot = () => render(
  <EditionModuleProvider edition={edition}>
    <OpenEditionRoot />
  </EditionModuleProvider>,
);

afterEach(() => {
  cleanup();
  window.history.replaceState({}, '', '/');
});

describe('OpenEditionRoot route access', () => {
  it('renders a public edition route logged out through the shared root', async () => {
    window.history.replaceState({}, '', '/public-page');

    renderRoot();

    const page = await screen.findByTestId('public-page');
    expect(page.closest(`[data-edition="frisket-edition:${editionId}"]`)).not.toBeNull();
    expect(screen.queryByTestId('sign-in')).toBeNull();
  });

  it('keeps an authenticated edition route behind sign-in', async () => {
    window.history.replaceState({}, '', '/private-page');

    renderRoot();

    expect(await screen.findByTestId('sign-in')).toBeVisible();
    expect(screen.queryByTestId('private-page')).toBeNull();
  });

  it('keeps an unknown route behind sign-in', async () => {
    window.history.replaceState({}, '', '/unknown-page');

    renderRoot();

    expect(await screen.findByTestId('sign-in')).toBeVisible();
    expect(screen.queryByTestId('public-page')).toBeNull();
  });
});
