export const PREFERENCES_KEY = 'frisket.personal_preferences.v1';

export type Preferences = {
  theme: 'system' | 'light' | 'dark';
};

const DEFAULT_PREFERENCES: Preferences = {
  theme: 'system',
};

export function readPersonalPreferences(): Preferences {
  const raw = localStorage.getItem(PREFERENCES_KEY);
  if (!raw) return DEFAULT_PREFERENCES;
  try {
    const parsed = JSON.parse(raw);
    const theme = parsed && typeof parsed === 'object'
      ? (parsed as Partial<Preferences>).theme
      : undefined;
    return theme === 'system' || theme === 'light' || theme === 'dark'
      ? { theme }
      : DEFAULT_PREFERENCES;
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

/** Resolve the 'system' preference to the OS's live prefers-color-scheme;
 *  light/dark pass through unchanged. `data-frisket-theme` (the attribute the grid's
 *  MutationObserver repaint and every `[data-frisket-theme="dark"]` CSS
 *  override key off) is always a resolved 'light' | 'dark' — it never carries
 *  the literal 'system' value, so no downstream consumer needs to learn a
 *  third state. */
export function resolveThemePreference(theme: Preferences['theme']): 'light' | 'dark' {
  if (theme !== 'system') return theme;
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

let systemThemeListenerAttached = false;

/** Attached once, lazily, the first time preferences are applied — a plain
 *  module-load side effect isn't safe (jsdom/vitest environments may not
 *  provide matchMedia). While the stored preference is 'system', an OS-level
 *  scheme flip re-applies preferences LIVE (re-resolving 'system') instead of
 *  waiting for next reload. */
function ensureSystemThemeListener(): void {
  if (systemThemeListenerAttached) return;
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
  systemThemeListenerAttached = true;
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const onChange = () => {
    const current = readPersonalPreferences();
    if (current.theme === 'system') applyPersonalPreferences(current);
  };
  if (typeof media.addEventListener === 'function') {
    media.addEventListener('change', onChange);
  } else if (typeof (media as unknown as { addListener?: (cb: () => void) => void }).addListener === 'function') {
    // Legacy Safari (pre-14) MediaQueryList API.
    (media as unknown as { addListener(cb: () => void): void }).addListener(onChange);
  }
}

export function applyPersonalPreferences(prefs: Preferences): void {
  const root = document.documentElement;
  root.dataset.frisketTheme = resolveThemePreference(prefs.theme);
  ensureSystemThemeListener();
}

export function applyStoredPersonalPreferences(): void {
  applyPersonalPreferences(readPersonalPreferences());
}
