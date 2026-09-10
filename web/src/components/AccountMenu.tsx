// Account / user menu — off the avatar in the global chrome bar and the Home top
// bar. The avatar carries a focus ring while the menu is open. Contents
// (top→bottom): a header (avatar + name + email when a real identity exists;
// local tier → "Local workspace", no email), Account settings, Preferences, API
// keys & models, a top-level Theme light/dark toggle row (one click, no submenu
// needed), an Appearance ▸ submenu (System / Light / Dark) wiring the EXISTING
// warm-dark theme machinery (settings/preferences.applyPersonalPreferences — the
// SAME mechanism the Personal settings ▸ Preferences page's Theme picker
// drives, reconciled here rather than reinvented. System follows the OS live via
// preferences.ts's matchMedia listener; the submenu shows the currently resolved
// light/dark under the System entry while it's active), Help & docs, Keyboard
// shortcuts, and — TIER HONESTY — Sign out ONLY on the auth tier.
//
// Account (personal) is one of three DISTINCT settings tiers; the other two are
// Project (off the project ▾ menu) and Workspace (off Home's left nav).

import { useEffect, useRef, useState } from 'react';
import {
  BookOpen,
  Keyboard,
  KeyRound,
  LogOut,
  Monitor,
  Moon,
  Palette,
  SlidersHorizontal,
  Sun,
  UserRound,
} from 'lucide-react';
import { signOut } from '../api/open';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import type { MeInfo, ProjectInfo } from '../api/types';
import { navigate } from '../routes';
import {
  clearSettingsProjectContext,
  writeSettingsProjectContext,
} from '../settings/settingsProjectContext';
import {
  PREFERENCES_KEY,
  applyPersonalPreferences,
  readPersonalPreferences,
  resolveThemePreference,
  type Preferences,
} from '../settings/preferences';

function setThemePreference(theme: Preferences['theme']): void {
  const next = { ...readPersonalPreferences(), theme };
  localStorage.setItem(PREFERENCES_KEY, JSON.stringify(next));
  applyPersonalPreferences(next);
}

export function AccountMenu({
  triggerTestId,
  project,
  identityMode,
  me,
  onOpenCommandPalette,
}: {
  /** Distinguishes the two mount points: `chrome-account` in the workbench
   *  chrome bar, `home-account` in the Home top bar. */
  triggerTestId: string;
  /** The in-scope project (chrome-account only; home-account has none). Also
   *  seeds the settings-project context (settingsProjectContext.ts) when
   *  navigating into PERSONAL settings from here, so the settings project
   *  switcher and chrome back-link auto-populate to it instead of forcing a
   *  manual project pick. */
  project?: ProjectInfo | null;
  identityMode: boolean;
  me: MeInfo | null;
  /** Opens the workbench command palette (chrome.openCommandPalette). Only
   *  ChromeBar (workspace chrome) has a palette to open; HomeScreen has none,
   *  so it omits this prop and the Keyboard shortcuts item hides. */
  onOpenCommandPalette?: () => void;
}) {
  const projectId = project?.id;
  const [open, setOpen] = useState(false);
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [theme, setTheme] = useState<Preferences['theme']>(
    () => readPersonalPreferences().theme,
  );
  // The System entry's "currently X" hint: resolved
  // fresh every render (a synchronous matchMedia read, not a side effect) so
  // it's never stale; a matchMedia listener below only forces a re-render
  // when the OS scheme flips — it doesn't derive/store the value itself.
  const [, forceThemeRecheck] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Top-layer popover: a plain ref+toggle
  // menu, so it carries the default Escape+outside dismissal only. The trigger
  // is ignored on outside-pointerdown so its own onClick keeps sole ownership
  // of the open/close toggle (a double-fire would otherwise close then reopen).
  useNativePopover(menuRef, () => setOpen(false), {
    enabled: open,
    ignoreSelector: `[data-testid="${triggerTestId}"]`,
  });
  // Fixed-position anchor now that the menu is a top-layer element — it no
  // longer inherits placement from `.account-menu-root`'s CSS positioned
  // ancestor (styles.css `.account-menu`, right-aligned under the trigger).
  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: 'right',
    width: 236,
    gap: 4,
  });

  useEffect(() => {
    if (theme !== 'system' || typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => forceThemeRecheck((n) => n + 1);
    media.addEventListener?.('change', onChange);
    return () => media.removeEventListener?.('change', onChange);
  }, [theme]);

  const resolvedTheme = resolveThemePreference(theme);

  const close = () => {
    setOpen(false);
    setAppearanceOpen(false);
  };

  const go = (route: Parameters<typeof navigate>[0]) => {
    close();
    navigate(route);
  };

  /** Navigate into a PERSONAL settings section, carrying the in-scope project
   *  (if any) as settings-project context. No project in scope (the Home
   *  avatar) clears any stale context so it can't hijack the route. */
  const goPersonal = (section: string) => {
    if (project) {
      writeSettingsProjectContext(project);
    } else {
      clearSettingsProjectContext();
    }
    go({ kind: 'settings', scope: 'personal', section });
  };

  const pickTheme = (next: Preferences['theme']) => {
    setThemePreference(next);
    setTheme(next);
    close();
  };

  /** Top-level light/dark toggle: flips
   *  the RESOLVED theme to its opposite through the exact same persistence
   *  mechanism as the Appearance submenu (setThemePreference above). Toggling
   *  while on 'system' pins an explicit light/dark — the System option stays
   *  available in the Appearance submenu. The menu stays open so the flip is
   *  visible in place. */
  const toggleTheme = () => {
    const next = resolvedTheme === 'dark' ? 'light' : 'dark';
    setThemePreference(next);
    setTheme(next);
  };

  const keysRoute: Parameters<typeof navigate>[0] = projectId
    ? { kind: 'settings', projectId, scope: 'project', section: 'ai-providers' }
    : { kind: 'settings', scope: 'organization', section: 'api-keys' };

  const name = me?.display_name?.trim() || (identityMode ? me?.email : null) || 'Local workspace';

  return (
    <div className="account-menu-root" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className={`chrome-account-btn account-menu-trigger${open ? ' open' : ''}`}
        data-testid={triggerTestId}
        title="Account & settings"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <UserRound size={15} />
      </button>
      {open && (
        <div
          ref={menuRef}
          className="menu-pop account-menu"
          data-testid="account-menu"
          role="menu"
          // Reset the UA popover centering (inset:0; margin:auto) so the
          // JS-anchored top-layer placement below applies instead.
          style={
            menuPos
              ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
              : { position: 'fixed', visibility: 'hidden' }
          }
        >
          <div className="account-menu-header" data-testid="account-menu-header">
            <span className="account-menu-avatar">
              <UserRound size={16} />
            </span>
            <span className="account-menu-identity">
              <span className="account-menu-name">{name}</span>
              {identityMode && me?.email && (
                <span className="account-menu-email">{me.email}</span>
              )}
            </span>
          </div>
          <div className="menu-sep" />
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            data-testid="account-settings"
            onClick={() => goPersonal('profile')}
          >
            <UserRound size={12} className="menu-item-icon" />
            <span className="menu-item-name">Account settings</span>
          </button>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            data-testid="account-preferences"
            onClick={() => goPersonal('preferences')}
          >
            <SlidersHorizontal size={12} className="menu-item-icon" />
            <span className="menu-item-name">Preferences</span>
          </button>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            data-testid="account-api-keys"
            onClick={() => go(keysRoute)}
          >
            <KeyRound size={12} className="menu-item-icon" />
            <span className="menu-item-name">API keys &amp; models</span>
          </button>
          <button
            type="button"
            className="menu-item"
            role="menuitemcheckbox"
            aria-checked={resolvedTheme === 'dark'}
            data-testid="account-theme-toggle"
            title={`Switch to ${resolvedTheme === 'dark' ? 'light' : 'dark'} theme`}
            onClick={toggleTheme}
          >
            {resolvedTheme === 'dark' ? (
              <Moon size={12} className="menu-item-icon" />
            ) : (
              <Sun size={12} className="menu-item-icon" />
            )}
            <span className="menu-item-name">Theme</span>
            <span className="menu-item-hint" data-testid="account-theme-toggle-state">
              {resolvedTheme === 'dark' ? 'Dark' : 'Light'}
            </span>
          </button>
          <div className="account-appearance">
            <button
              type="button"
              className="menu-item"
              role="menuitem"
              aria-haspopup="menu"
              aria-expanded={appearanceOpen}
              data-testid="account-appearance"
              onClick={() => setAppearanceOpen((v) => !v)}
            >
              <Palette size={12} className="menu-item-icon" />
              <span className="menu-item-name">Appearance</span>
              <span className="menu-item-flyout-caret">▸</span>
            </button>
            {appearanceOpen && (
              <div className="menu-nested account-appearance-submenu" role="menu">
                <button
                  type="button"
                  className={`menu-item${theme === 'system' ? ' current' : ''}`}
                  role="menuitemradio"
                  aria-checked={theme === 'system'}
                  data-testid="account-appearance-system"
                  onClick={() => pickTheme('system')}
                >
                  <Monitor size={12} className="menu-item-icon" />
                  <span className="menu-item-name">System</span>
                  {theme === 'system' && (
                    <span
                      className="menu-item-hint"
                      data-testid="account-appearance-system-effective"
                    >
                      {resolvedTheme === 'dark' ? 'Dark now' : 'Light now'}
                    </span>
                  )}
                </button>
                <button
                  type="button"
                  className={`menu-item${theme === 'light' ? ' current' : ''}`}
                  role="menuitemradio"
                  aria-checked={theme === 'light'}
                  data-testid="account-appearance-light"
                  onClick={() => pickTheme('light')}
                >
                  <Sun size={12} className="menu-item-icon" />
                  <span className="menu-item-name">Light</span>
                </button>
                <button
                  type="button"
                  className={`menu-item${theme === 'dark' ? ' current' : ''}`}
                  role="menuitemradio"
                  aria-checked={theme === 'dark'}
                  data-testid="account-appearance-dark"
                  onClick={() => pickTheme('dark')}
                >
                  <Moon size={12} className="menu-item-icon" />
                  <span className="menu-item-name">Dark</span>
                </button>
              </div>
            )}
          </div>
          <div className="menu-sep" />
          <a
            className="menu-item"
            role="menuitem"
            data-testid="account-help"
            href="https://frisket.dev/docs"
            target="_blank"
            rel="noreferrer"
            onClick={close}
          >
            <BookOpen size={12} className="menu-item-icon" />
            <span className="menu-item-name">Help &amp; docs</span>
          </a>
          {onOpenCommandPalette && (
            <button
              type="button"
              className="menu-item"
              role="menuitem"
              data-testid="account-shortcuts"
              onClick={() => {
                close();
                onOpenCommandPalette();
              }}
            >
              <Keyboard size={12} className="menu-item-icon" />
              <span className="menu-item-name">Keyboard shortcuts</span>
              <span className="menu-item-kbd">?</span>
            </button>
          )}
          {/* TIER HONESTY: Sign out exists ONLY on the auth tier — the local
              tier has no session to end (/api/me 404 convention). */}
          {identityMode && (
            <>
              <div className="menu-sep" />
              <button
                type="button"
                className="menu-item menu-item-danger"
                role="menuitem"
                data-testid="account-sign-out"
                onClick={() => {
                  close();
                  void signOut().then(() => window.location.reload());
                }}
              >
                <LogOut size={12} className="menu-item-icon" />
                <span className="menu-item-name">Sign out</span>
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
