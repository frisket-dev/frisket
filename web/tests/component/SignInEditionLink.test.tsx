// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  requestMagicLink: vi.fn(),
  signInWithPassword: vi.fn(),
  getRuntimeConfig: vi.fn(),
}));

vi.mock('../../src/api/open', () => ({
  getRuntimeConfig: mocks.getRuntimeConfig,
}));
vi.mock('../../src/api/raw/browserAuth', () => ({
  oidcSignInUrl: (provider: string) => `/auth/oidc/${provider}`,
}));
vi.mock('../../src/api/browserAuth', () => ({
  requestMagicLink: mocks.requestMagicLink,
  signInWithPassword: mocks.signInWithPassword,
}));
vi.mock('../../src/instanceIdentity', () => ({
  useInstanceIdentity: () => ({ display_name: 'Example operator', support_contact: null }),
}));
import { SignIn } from '../../src/components/SignIn';
import {
  defineEditionModule,
  EditionModuleProvider,
} from '../../src/editions/module';



afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const runtimeConfig = {
  cache_mode: 'replay',
  live_calls_possible: false,
  cache_mode_editable: false,
  email_from_address: 'notifications@example.test',
  email_from_name: 'frisket',
  auth_methods: { password: true, magic_link: true, oidc: [] },
};

const edition = defineEditionModule({
  descriptor: {
    id: 'sign-in-link-test',
    capabilities: {
      configurableNotificationDestinations: false,
      configurableNotificationEmail: false,
      identity: true,
      team: true,
    },
  },
  routes: [{
    id: 'join-example',
    path: '/join-example',
    access: 'public',
    discoverableFrom: 'sign-in',
    stableHook: 'sign-in-join-example',
    linkLabel: 'Ask Example operator for access',
  }],
});

const renderSignIn = () => render(
  <EditionModuleProvider edition={edition}>
    <SignIn />
  </EditionModuleProvider>,
);

describe('SignIn edition links', () => {
  it('renders the route-provided label and no invented provider', async () => {
    mocks.getRuntimeConfig.mockResolvedValue(runtimeConfig);
    renderSignIn();

    const link = await screen.findByTestId('sign-in-join-example');
    expect(link).toHaveTextContent('Ask Example operator for access');
    expect(link).toHaveAttribute('href', '/join-example');
    expect(screen.queryByText(/^Request access$/)).toBeNull();
    expect(screen.queryByTestId('sign-in-oidc-google')).toBeNull();
  });

  it('shows only runtime-advertised OIDC providers', async () => {
    mocks.getRuntimeConfig.mockResolvedValue({
      ...runtimeConfig,
      auth_methods: {
        ...runtimeConfig.auth_methods,
        oidc: [{ id: 'google', label: 'Google' }],
      },
    });
    renderSignIn();

    expect(await screen.findByTestId('sign-in-oidc-google')).toHaveAttribute(
      'href',
      '/auth/oidc/google',
    );
  });

  it('moves focus to the revealed magic-link address field', async () => {
    mocks.getRuntimeConfig.mockResolvedValue(runtimeConfig);
    renderSignIn();

    fireEvent.click(await screen.findByTestId('sign-in-magic-toggle'));

    const emailFields = await screen.findAllByRole('textbox', { name: 'Email' });
    expect(emailFields.at(-1)).toHaveFocus();
  });

  it('delegates the entered address to the named browser auth operation', async () => {
    mocks.getRuntimeConfig.mockResolvedValue(runtimeConfig);
    mocks.requestMagicLink.mockResolvedValue(undefined);
    renderSignIn();

    fireEvent.click(await screen.findByTestId('sign-in-magic-toggle'));
    fireEvent.change(screen.getByTestId('sign-in-email'), {
      target: { value: ' reporter@example.test ' },
    });
    fireEvent.click(screen.getByTestId('sign-in-submit'));

    expect(mocks.requestMagicLink).toHaveBeenCalledWith('reporter@example.test');
    expect(await screen.findByTestId('sign-in-sent')).toBeVisible();
  });
});
