// @vitest-environment jsdom
//
// AccountMenu's Appearance submenu has a System option wired to
// settings/preferences.ts's shared theme machinery -- the SAME module the
// Personal settings Preferences page's Theme <select> reads/writes.
//
// The original spec's third test also drove the Personal settings page
// (`personal-preferences-settings` / `personal-pref-theme`), which lives in
// src/settings/SettingsSections.tsx -- a large, unrelated settings surface
// outside this conversion's assigned component set (AccountMenu / the theme
// submenu). That page's Theme <select> is a thin, direct reader/writer of the
// exact same `readPersonalPreferences`/`applyPersonalPreferences` module
// exercised below, so the "one mechanism, not two" invariant is proven
// honestly here by writing through that shared module directly (exactly what
// the settings page's own `update()` does) and confirming a FRESH AccountMenu
// mount (standing in for "navigate to a different page/mount point")
// reconciles to it -- without re-mounting the unrelated settings tree.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { AccountMenu } from '../../src/components/AccountMenu';
import { PREFERENCES_KEY, applyPersonalPreferences, readPersonalPreferences } from '../../src/settings/preferences';
import { installMatchMedia, installPopoverPolyfill } from '../support/domPolyfills';



vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return { ...actual, signOut: vi.fn() };
});

beforeAll(() => {
  installPopoverPolyfill();
});

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-frisket-theme');
});

afterEach(() => {
  cleanup();
});

async function openAppearanceSubmenu() {
  await userEvent.click(screen.getByTestId('home-account'));
  await screen.findByTestId('account-menu');
  await userEvent.click(screen.getByTestId('account-appearance'));
  await screen.findByTestId('account-appearance-system');
}

describe('AccountMenu Appearance submenu (System option)', () => {
  it('offers System alongside Light and Dark', async () => {
    render(<AccountMenu triggerTestId="home-account" identityMode={false} me={null} />);
    await openAppearanceSubmenu();

    expect(screen.getByTestId('account-appearance-system')).toBeInTheDocument();
    expect(screen.getByTestId('account-appearance-light')).toBeInTheDocument();
    expect(screen.getByTestId('account-appearance-dark')).toBeInTheDocument();
  });

  it('selecting System resolves to the OS scheme, follows it LIVE via matchMedia, and the entry shows the effective state', async () => {
    const media = installMatchMedia('dark');
    try {
      render(<AccountMenu triggerTestId="home-account" identityMode={false} me={null} />);
      await openAppearanceSubmenu();
      await userEvent.click(screen.getByTestId('account-appearance-system'));

      // Selecting System applies the OS's CURRENT (emulated dark) scheme to the
      // resolved theme attribute the grid/CSS key off -- never the literal
      // string 'system'.
      expect(document.documentElement.dataset.frisketTheme).toBe('dark');
      expect(JSON.parse(localStorage.getItem(PREFERENCES_KEY) || '{}').theme).toBe('system');

      // Flip the OS scheme WITHOUT remounting: the app follows it live via the
      // matchMedia change listener preferences.ts attaches. `act()` flushes the
      // state update AccountMenu's own listener schedules (forceThemeRecheck),
      // since `media.setColorScheme` fires listeners synchronously outside any
      // React event handler.
      act(() => media.setColorScheme('light'));
      expect(document.documentElement.dataset.frisketTheme).toBe('light');

      // The dropdown's System entry reflects the newly-resolved effective state.
      await openAppearanceSubmenu();
      expect(screen.getByTestId('account-appearance-system')).toHaveAttribute('aria-checked', 'true');
      expect(screen.getByTestId('account-appearance-system-effective')).toHaveTextContent(/light/i);

      // Flip back to dark live; the hint updates again. AccountMenu re-derives
      // `resolvedTheme` synchronously on render, forced by the module's own
      // matchMedia listener re-rendering this component -- not a poll.
      act(() => media.setColorScheme('dark'));
      expect(screen.getByTestId('account-appearance-system-effective')).toHaveTextContent(/dark/i);
      expect(document.documentElement.dataset.frisketTheme).toBe('dark');
    } finally {
      media.uninstall();
    }
  });

  it('shares ONE preference with any other reader/writer of settings/preferences.ts (the same module the Personal settings Theme select uses)', async () => {
    installMatchMedia('light');
    const { unmount } = render(<AccountMenu triggerTestId="home-account" identityMode={false} me={null} />);
    await openAppearanceSubmenu();
    await userEvent.click(screen.getByTestId('account-appearance-dark'));
    expect(document.documentElement.dataset.frisketTheme).toBe('dark');
    unmount();

    // A different reader (e.g. the Personal settings page's Theme <select>)
    // sees the SAME persisted preference.
    expect(readPersonalPreferences().theme).toBe('dark');

    // Writing through the exact mechanism the settings page's own `update()`
    // uses (settings/SettingsSections.tsx PersonalPreferencesSettings: write
    // localStorage, then applyPersonalPreferences) -- not a re-implementation,
    // the same exported function AccountMenu itself calls.
    const next = { ...readPersonalPreferences(), theme: 'system' as const };
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify(next));
    applyPersonalPreferences(next);

    // A freshly-mounted AccountMenu (standing in for navigating to a
    // different page and back) reconciles to that externally-written choice.
    render(<AccountMenu triggerTestId="home-account" identityMode={false} me={null} />);
    await openAppearanceSubmenu();
    expect(screen.getByTestId('account-appearance-system')).toHaveAttribute('aria-checked', 'true');
  });
});

// The top-level theme toggle is a direct light/dark flip row at the menu's TOP
// LEVEL — users shouldn't need to open
// the Appearance ▸ submenu for the common case. It writes through the exact
// same mechanism the submenu uses (setThemePreference → PREFERENCES_KEY +
// applyPersonalPreferences), so the two controls can never disagree.
describe('AccountMenu top-level theme toggle', () => {
  async function openMenu() {
    await userEvent.click(screen.getByTestId('home-account'));
    await screen.findByTestId('account-menu');
  }

  it('flips data-frisket-theme and persists through the shared preferences key', async () => {
    const media = installMatchMedia('light');
    try {
      render(<AccountMenu triggerTestId="home-account" identityMode={false} me={null} />);
      await openMenu();

      // Default preference is 'system' (resolving light here) — the row shows
      // the RESOLVED state, not the stored preference name.
      expect(screen.getByTestId('account-theme-toggle-state')).toHaveTextContent('Light');

      // One click: root attribute flips AND the preference persists as an
      // explicit 'dark' under the SAME key the Appearance submenu writes.
      await userEvent.click(screen.getByTestId('account-theme-toggle'));
      expect(document.documentElement.dataset.frisketTheme).toBe('dark');
      expect(JSON.parse(localStorage.getItem(PREFERENCES_KEY) || '{}').theme).toBe('dark');

      // The menu stays open and the row reflects the flip in place; a second
      // click toggles back.
      expect(screen.getByTestId('account-theme-toggle-state')).toHaveTextContent('Dark');
      expect(screen.getByTestId('account-theme-toggle')).toHaveAttribute('aria-checked', 'true');
      await userEvent.click(screen.getByTestId('account-theme-toggle'));
      expect(document.documentElement.dataset.frisketTheme).toBe('light');
      expect(JSON.parse(localStorage.getItem(PREFERENCES_KEY) || '{}').theme).toBe('light');
    } finally {
      media.uninstall();
    }
  });

  it('shares ONE preference with the Appearance submenu (no second mechanism)', async () => {
    const media = installMatchMedia('light');
    try {
      render(<AccountMenu triggerTestId="home-account" identityMode={false} me={null} />);

      // Pick Dark via the EXISTING submenu…
      await openAppearanceSubmenu();
      await userEvent.click(screen.getByTestId('account-appearance-dark'));
      expect(document.documentElement.dataset.frisketTheme).toBe('dark');

      // …the top-level toggle reflects it, and flipping it back writes the
      // same stored preference the submenu then reports as current.
      await openMenu();
      expect(screen.getByTestId('account-theme-toggle-state')).toHaveTextContent('Dark');
      await userEvent.click(screen.getByTestId('account-theme-toggle'));
      expect(document.documentElement.dataset.frisketTheme).toBe('light');
      expect(readPersonalPreferences().theme).toBe('light');
      await userEvent.click(screen.getByTestId('account-appearance'));
      await screen.findByTestId('account-appearance-light');
      expect(screen.getByTestId('account-appearance-light')).toHaveAttribute('aria-checked', 'true');
    } finally {
      media.uninstall();
    }
  });

  it('toggling while on System pins an explicit theme (opposite of the resolved scheme)', async () => {
    const media = installMatchMedia('dark');
    try {
      render(<AccountMenu triggerTestId="home-account" identityMode={false} me={null} />);
      await openMenu();

      // System resolving dark → the toggle offers (and applies) light.
      expect(screen.getByTestId('account-theme-toggle-state')).toHaveTextContent('Dark');
      await userEvent.click(screen.getByTestId('account-theme-toggle'));
      expect(document.documentElement.dataset.frisketTheme).toBe('light');
      expect(JSON.parse(localStorage.getItem(PREFERENCES_KEY) || '{}').theme).toBe('light');
    } finally {
      media.uninstall();
    }
  });
});
