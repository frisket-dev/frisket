// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  requestMagicLink: vi.fn(),
  signInWithPassword: vi.fn(),
  getRuntimeConfig: vi.fn(),
  continueAfterBrowserSignIn: vi.fn(),
}));

vi.mock('../../src/api/open', () => ({
  getRuntimeConfig: mocks.getRuntimeConfig,
}));
vi.mock('../../src/api/raw/browserAuth', () => ({
  oidcSignInUrl: (provider: string, returnTo?: string) => `/auth/oidc/${provider}${returnTo && returnTo !== '/'
    ? `?returnTo=${encodeURIComponent(returnTo)}` : ''}`,
  continueAfterBrowserSignIn: mocks.continueAfterBrowserSignIn,
}));
vi.mock('../../src/api/browserAuth', () => ({
  requestMagicLink: mocks.requestMagicLink,
  signInWithPassword: mocks.signInWithPassword,
}));
vi.mock('../../src/instanceIdentity', () => ({
  useInstanceIdentity: () => ({ display_name: 'Example operator', support_contact: null }),
}));
import { SignIn } from '../../src/components/SignIn';
import { resumePendingSignIn } from '../../src/components/authReturnTo';
import {
  defineEditionModule,
  EditionModuleProvider,
} from '../../src/editions/module';



afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState({}, '', '/');
  window.sessionStorage.clear();
  window.localStorage.clear();
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
      skills: false,
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

  it.each(['/device?user_code=KQ4F-7M2D', '/admin/signup-requests#pending'])('returns a password sign-in to %s', async (destination) => {
    window.history.replaceState({}, '', destination);
    mocks.getRuntimeConfig.mockResolvedValue(runtimeConfig);
    mocks.signInWithPassword.mockResolvedValue(undefined);
    renderSignIn();

    fireEvent.change(await screen.findByTestId('sign-in-email'), {
      target: { value: 'person@example.test' },
    });
    fireEvent.change(screen.getByTestId('sign-in-password'), {
      target: { value: 'password' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^Sign in$/ }));

    await waitFor(() => {
      expect(mocks.signInWithPassword).toHaveBeenCalledWith('person@example.test', 'password');
      expect(mocks.continueAfterBrowserSignIn).toHaveBeenCalledWith(
        destination,
      );
    });
  });

  it.each(['/device?user_code=KQ4F-7M2D', '/admin/signup-requests'])('preserves %s through an OIDC callback', async (destination) => {
    window.history.replaceState({}, '', destination);
    mocks.getRuntimeConfig.mockResolvedValue({
      ...runtimeConfig,
      auth_methods: {
        ...runtimeConfig.auth_methods,
        oidc: [{ id: 'google', label: 'Google' }],
      },
    });
    renderSignIn();

    expect(await screen.findByTestId('sign-in-oidc-google')).toHaveAttribute(
      'href', `/auth/oidc/google?${new URLSearchParams({ returnTo: destination })}`,
    );
    expect(resumePendingSignIn({ pathname: '/', search: '', hash: '' })).toBe(destination);
  });

  it('still renders sign-in when localStorage access is denied', async () => {
    window.history.replaceState({}, '', '/admin/signup-requests');
    mocks.getRuntimeConfig.mockResolvedValue(runtimeConfig);
    const denied = vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
    try {
      renderSignIn();
      expect(await screen.findByTestId('sign-in-email')).toBeVisible();
    } finally {
      denied.mockRestore();
    }
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

  it.each(['/device?user_code=KQ4F-7M2D', '/admin/signup-requests'])('resumes %s after a magic-link callback returns to root', async (destination) => {
    window.history.replaceState({}, '', destination);
    mocks.getRuntimeConfig.mockResolvedValue(runtimeConfig);
    mocks.requestMagicLink.mockResolvedValue(undefined);
    renderSignIn();

    fireEvent.click(await screen.findByTestId('sign-in-magic-toggle'));
    fireEvent.change(screen.getByTestId('sign-in-email'), {
      target: { value: 'person@example.test' },
    });
    fireEvent.click(screen.getByTestId('sign-in-submit'));
    expect(await screen.findByTestId('sign-in-sent')).toBeVisible();

    expect(resumePendingSignIn(
      { pathname: '/', search: '', hash: '' },
      window.localStorage,
    )).toBe(destination);
  });
});
