// Shared boilerplate for the *-screenshots.spec.ts review-gallery generators
// (task e2e-helper-consolidation-v1 / P5). These specs are NOT golden-image
// tests — there is no toHaveScreenshot/toMatchSnapshot comparison anywhere in
// the suite (grep confirms) — they write review artifacts to web/screenshots/
// for a human to look at. Consolidating their setup mechanics is therefore
// safe: it cannot change what any spec asserts.

import { mkdirSync } from 'node:fs';
import path from 'node:path';

import type { Page } from '@playwright/test';

// The two viewport sizes the gallery specs actually use. SHOT_VIEWPORT is the
// majority (11 specs); SHOT_VIEWPORT_NARROW is the OCR/transcribe-compare +
// chips-segmented family.
export const SHOT_VIEWPORT = { width: 1680, height: 960 };
export const SHOT_VIEWPORT_NARROW = { width: 1440, height: 900 };

/** Playwright runs with cwd = web/, so this resolves into web/screenshots/<sub>/. */
export function shotsDir(...segments: string[]): string {
  return path.resolve(process.cwd(), 'screenshots', ...segments);
}

export function ensureShotsDir(dir: string): void {
  mkdirSync(dir, { recursive: true });
}

// ---------------------------------------------------------------------------
// setTheme — THE VERIFIED-LIVE MECHANISM (investigated, not guessed).
//
// The app's own theming code (web/src/settings/preferences.ts
// applyPersonalPreferences) does exactly one thing to switch theme:
//   document.documentElement.dataset.frisketTheme = prefs.theme;
// That single `data-frisket-theme` attribute is:
//   - the only theme selector styles.css defines (`:root[data-frisket-theme=
//     "dark"]`, styles.css:64);
//   - the only attribute SheetGrid.tsx's MutationObserver watches to re-derive
//     the canvas grid theme live (attributeFilter: ['data-frisket-theme'],
//     SheetGrid.tsx:478).
// So directly setting `data-frisket-theme` via page.evaluate is a faithful,
// zero-indirection stand-in for the real preference-application code path,
// and — unlike seeding localStorage before navigation — it can be called
// repeatedly within a single already-loaded test to flip a spec through
// light/dark for successive screenshots (what most of these specs do).
//
// `data-theme` (no `frisket-` prefix) has no consumers anywhere in web/src, so
// setting it beside data-frisket-theme only obscures which attribute works.
//
// The THIRD mechanism seen in the wild — seeding
// localStorage['frisket.personal_preferences.v1'] via page.addInitScript,
// which the shared edition mount applies before React renders —
// is the real production load path, but it only takes effect once at mount
// and cannot re-flip an already-loaded page. It is also the ONLY thing that
// feeds AccountMenu's Appearance-submenu "currently selected theme" render
// (AccountMenu.tsx reads readPersonalPreferences().theme), so any spec that
// screenshots that submenu needs the localStorage seed, not this helper — see
// the allowlisted exception in workbench-ia-shell-screenshots.spec.ts.
export async function setTheme(page: Page, theme: 'light' | 'dark'): Promise<void> {
  await page.evaluate((value) => {
    document.documentElement.setAttribute('data-frisket-theme', value);
  }, theme);
}

/** Bind a plain full-page `shoot(page, name)` to one output directory. */
export function makeShoot(outDir: string) {
  return async (page: Page, name: string): Promise<void> => {
    await page.screenshot({ path: path.join(outDir, name), animations: 'disabled' });
  };
}

/** One-off element screenshot (a specific testid's locator, not the full page). */
export async function shootElement(
  page: Page,
  outDir: string,
  testId: string,
  name: string,
): Promise<void> {
  await page.getByTestId(testId).screenshot({ path: path.join(outDir, name), animations: 'disabled' });
}

/** Bind a fixed-testid `shoot(page, name)` to one output directory (the
 *  action-drawer capture shape reused verbatim by two galleries). */
export function makeElementShoot(outDir: string, testId: string) {
  return (page: Page, name: string): Promise<void> => shootElement(page, outDir, testId, name);
}
