import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

// The static token-value half — the warm-paper palette (light + dark) and the
// self-hosted IBM Plex / no-Google-Fonts source — lives in web/tests/unit/
// tokens.test.ts, and the pure readGridTheme() derivation lives in web/tests/
// component/gridTheme.test.tsx. This file covers genuine
// browser-runtime properties a node/jsdom test can't express — that the Plex
// woff2 faces actually LOAD self-hosted with zero Google Fonts network fetch,
// and that the LIVE canvas grid publishes + re-derives window.__frisketGridTheme
// through its React effect + MutationObserver on a theme flip.

test('workbench IA tokens: IBM Plex is self-hosted and body adopts it', async ({ page }) => {
  const googleFontRequests: string[] = [];
  page.on('request', (req) => {
    const url = req.url();
    if (url.includes('fonts.googleapis.com') || url.includes('fonts.gstatic.com')) {
      googleFontRequests.push(url);
    }
  });

  await page.goto('/');
  // Body must render in IBM Plex Sans (Inter is gone).
  const bodyFont = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  expect(bodyFont.replace(/['"]/g, '')).toMatch(/^IBM Plex Sans/);

  // The Plex faces must actually be loadable, self-hosted — no Google Fonts
  // fetch. Force the loads deterministically (fonts are lazy @font-face rules,
  // so document.fonts.ready can resolve before a given weight starts loading).
  const fontState = await page.evaluate(async () => {
    await Promise.all([
      document.fonts.load("400 12px 'IBM Plex Sans'"),
      document.fonts.load("600 12px 'IBM Plex Sans'"),
      document.fonts.load("400 12px 'IBM Plex Mono'"),
    ]);
    await document.fonts.ready;
    const sansLoaded = document.fonts.check("12px 'IBM Plex Sans'");
    const monoLoaded = document.fonts.check("12px 'IBM Plex Mono'");
    const families = Array.from(document.fonts).map((f) => f.family);
    return { sansLoaded, monoLoaded, families };
  });
  expect(fontState.sansLoaded).toBe(true);
  expect(fontState.monoLoaded).toBe(true);
  expect(fontState.families.some((f) => f.replace(/['"]/g, '') === 'IBM Plex Sans')).toBe(true);
  expect(fontState.families.some((f) => f.replace(/['"]/g, '') === 'IBM Plex Mono')).toBe(true);
  expect(googleFontRequests).toEqual([]);
});

test('workbench IA tokens: grid canvas theme is derived from CSS tokens', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-ia-tokens'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nSyracuse,open\nAlbany,closed\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await page.evaluate(() => {
    document.documentElement.dataset.frisketTheme = 'light';
  });

  // The grid exposes its live theme for drift-guarding against the CSS tokens.
  // Publication happens in a React effect + MutationObserver callback, so poll
  // rather than assuming it is synchronous with grid visibility / theme flips.
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            (window as unknown as { __frisketGridTheme?: Record<string, unknown> })
              .__frisketGridTheme?.bgCell,
        ),
      { message: 'grid must publish window.__frisketGridTheme for the light theme' },
    )
    .toBe('#ffffff');
  const lightProbe = await page.evaluate(() => {
    const g = (window as unknown as { __frisketGridTheme?: Record<string, unknown> })
      .__frisketGridTheme;
    const s = getComputedStyle(document.documentElement);
    return {
      theme: g,
      bg: s.getPropertyValue('--bg').trim(),
      accent: s.getPropertyValue('--accent').trim(),
      text: s.getPropertyValue('--text').trim(),
    };
  });
  expect(lightProbe.theme, 'grid must publish window.__frisketGridTheme').toBeTruthy();
  expect(lightProbe.theme?.bgCell).toBe(lightProbe.bg);
  expect(lightProbe.theme?.accentColor).toBe(lightProbe.accent);
  expect(lightProbe.theme?.textDark).toBe(lightProbe.text);
  expect(String(lightProbe.theme?.fontFamily).replace(/['"]/g, '')).toMatch(/^IBM Plex Sans/);

  // Flipping the theme must re-derive the canvas theme (no static drift).
  // The re-derive runs in a MutationObserver callback — poll for the update.
  await page.evaluate(() => {
    document.documentElement.dataset.frisketTheme = 'dark';
  });
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            (window as unknown as { __frisketGridTheme?: Record<string, unknown> })
              .__frisketGridTheme?.bgCell,
        ),
      { message: 'grid theme must re-derive after the dark flip' },
    )
    .toBe('#1d1b17');
  const darkBg = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
  );
  expect(darkBg).toBe('#1d1b17');
});
