// @vitest-environment jsdom
//
// window.__frisketGridTheme is derived from the CSS token
// custom properties (so the <canvas> grid can't drift from styles.css) and
// re-derives on a theme flip. readGridTheme() is exactly that derivation — a
// pure getComputedStyle read — so we drive the CSS vars directly and assert the
// mapping, plus that flipping a var re-derives.

import { describe, expect, it } from 'vitest';
import { readAiColumnTheme, readGridCellPalette, readGridTheme } from '../../src/grid/gridTheme';

function setToken(name: string, value: string): void {
  document.documentElement.style.setProperty(name, value);
}

describe('grid canvas theme derivation', () => {
  it('derives the error, empty, and pending-cell palette from the light CSS tokens', () => {
    setToken('--red', '#b3392f');
    setToken('--text-light', '#8f8879');
    setToken('--ai', '#7c5ce6');
    setToken('--ai-bg', '#eee7fb');

    expect(readGridCellPalette()).toEqual({
      error: { textDark: '#b3392f', textLight: '#b3392f' },
      empty: { textDark: '#8f8879', textLight: '#8f8879' },
      pending: { background: '#eee7fb', highlight: '#7c5ce6' },
    });
  });

  it('re-derives the error, empty, and pending-cell palette after a dark token flip', () => {
    setToken('--red', '#e0776b');
    setToken('--text-light', '#8a8375');
    setToken('--ai', '#b3a0f2');
    setToken('--ai-bg', '#2f2947');

    expect(readGridCellPalette()).toEqual({
      error: { textDark: '#e0776b', textLight: '#e0776b' },
      empty: { textDark: '#8a8375', textLight: '#8a8375' },
      pending: { background: '#2f2947', highlight: '#b3a0f2' },
    });
  });

  it('derives base grid colors from the CSS token palette', () => {
    setToken('--bg', '#ffffff');
    setToken('--accent', '#4b53d9');
    setToken('--text', '#211f1b');
    setToken('--font', "'IBM Plex Sans', system-ui, sans-serif");

    const theme = readGridTheme();
    expect(theme.bgCell).toBe('#ffffff');
    expect(theme.accentColor).toBe('#4b53d9');
    expect(theme.textDark).toBe('#211f1b');
    expect(String(theme.fontFamily).replace(/['"]/g, '')).toMatch(/^IBM Plex Sans/);
  });

  it('re-derives (no static drift) when the theme flips a token', () => {
    setToken('--bg', '#ffffff');
    expect(readGridTheme().bgCell).toBe('#ffffff');

    // A dark flip only rewrites the CSS custom properties; the grid theme must
    // follow, never a baked-in literal.
    setToken('--bg', '#1d1b17');
    expect(readGridTheme().bgCell).toBe('#1d1b17');
  });

  it('derives the AI-column tint and readable header foreground from CSS tokens', () => {
    setToken('--ai', '#7c5ce6');
    setToken('--ai-bg', '#eee7fb');
    setToken('--text', '#211f1b');
    const ai = readAiColumnTheme();
    expect(ai.bgCell).toBe('#eee7fb');
    expect(ai.bgIconHeader).toBe('#7c5ce6');
    expect(ai.textHeader).toBe('#211f1b');
  });

  // SheetGrid.tsx's op-group banner (getGroupDetails' overrideTheme — the
  // map.extract/research.answer header band) used to carry its OWN hardcoded
  // hex triplet (#f4f0ff/#ebe3fb/#563d9a) instead of reading these tokens: it
  // never re-derived on a dark-mode flip (a light-lavender/near-white banner
  // over a dark grid), and diverged from a column header that wasn't part of
  // a group (no override at all, so it stayed correctly dark) — the "one
  // header light, its neighbor dark" symptom. The fix reuses THIS SAME
  // object as the banner's overrideTheme, so bgHeader/bgHeaderHovered/
  // textGroupHeader has to remain readable against that wash and move with
  // its semantic text role.
  it('derives the header/banner wash and group foreground from CSS tokens', () => {
    setToken('--ai', '#7c5ce6');
    setToken('--ai-bg', '#eee7fb');
    setToken('--text-mid', '#57534a');
    const ai = readAiColumnTheme();
    expect(ai.bgHeader).toBe('#eee7fb');
    expect(ai.textGroupHeader).toBe('#57534a');
    expect(ai.bgHeaderHovered).toMatch(/^#[0-9a-f]{6}$/i);
    expect(ai.bgHeaderHovered).not.toBe(ai.bgHeader);
  });

  it('re-derives the header/banner wash on a dark-mode flip instead of staying pinned to the light hex', () => {
    setToken('--ai', '#7c5ce6');
    setToken('--ai-bg', '#eee7fb');
    expect(readAiColumnTheme().bgHeader).toBe('#eee7fb');

    // styles.css's dark :root values — a subdued dark-purple wash, not the
    // light-mode #f4f0ff/#ebe3fb/#563d9a the old hardcoded banner literal
    // rendered regardless of theme.
    setToken('--ai', '#b3a0f2');
    setToken('--ai-bg', '#2f2947');
    setToken('--text-mid', '#b7b0a2');
    const dark = readAiColumnTheme();
    expect(dark.bgHeader).toBe('#2f2947');
    expect(dark.textGroupHeader).toBe('#b7b0a2');
    expect(dark.bgHeader).not.toBe('#f4f0ff');
    expect(dark.textGroupHeader).not.toBe('#563d9a');
  });
});
