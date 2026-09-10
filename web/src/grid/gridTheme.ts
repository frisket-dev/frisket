// Glide Data Grid renders to <canvas>, so it cannot inherit CSS custom
// properties the way the DOM does. To keep the canvas grid from drifting away
// from the warm-paper token palette in web/src/styles.css, both the base grid
// theme and the AI-column override are DERIVED at runtime from the same CSS
// custom properties (getComputedStyle at mount + on `data-frisket-theme`
// change). There is intentionally no static palette literal here: the values
// live once, in :root, and are read from there.
import type { Theme } from '@glideapps/glide-data-grid';

/** CSS-token snapshot for the non-structural canvas cell treatments. */
export interface GridCellPalette {
  error: Pick<Theme, 'textDark' | 'textLight'>;
  empty: Pick<Theme, 'textDark' | 'textLight'>;
  pending: {
    background: string;
    highlight: string;
  };
}

function tokens(): CSSStyleDeclaration | null {
  // Non-DOM guard (node-side tooling, future SSR): fall back to the documented
  // token defaults instead of throwing.
  if (typeof document === 'undefined' || typeof getComputedStyle !== 'function') {
    return null;
  }
  return getComputedStyle(document.documentElement);
}

function v(styles: CSSStyleDeclaration | null, name: string, fallback: string): string {
  if (styles === null) return fallback;
  const raw = styles.getPropertyValue(name).trim();
  return raw.length > 0 ? raw : fallback;
}

/** #rrggbb -> [r, g, b], or null for anything else (a named color, a stale
 *  token value mid-migration, …) — `mixHex` falls back to `b` unmixed rather
 *  than throw when a token isn't a plain hex literal. */
function parseHex(hex: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Linear channel mix of two hex colors — used only to derive the AI header/
 *  banner HOVER shade from the same two tokens (`--ai`, `--ai-bg`) instead of
 *  a third hardcoded literal, so the hover state stays theme-reactive too. */
function mixHex(a: string, b: string, t: number): string {
  const pa = parseHex(a);
  const pb = parseHex(b);
  if (!pa || !pb) return b;
  const mix = (x: number, y: number) => Math.round(x + (y - x) * t);
  return `#${[mix(pa[0], pb[0]), mix(pa[1], pb[1]), mix(pa[2], pb[2])]
    .map((n) => n.toString(16).padStart(2, '0'))
    .join('')}`;
}

/** Base grid theme, derived from the CSS token palette. */
export function readGridTheme(): Partial<Theme> {
  const s = tokens();
  return {
    accentColor: v(s, '--accent', '#4b53d9'),
    accentLight: v(s, '--accent-bg', '#ecedfb'),
    textDark: v(s, '--text', '#211f1b'),
    textMedium: v(s, '--text-mid', '#57534a'),
    textLight: v(s, '--text-light', '#8f8879'),
    textHeader: v(s, '--text-mid', '#57534a'),
    bgCell: v(s, '--bg', '#ffffff'),
    bgCellMedium: v(s, '--bg-panel', '#faf8f4'),
    bgHeader: v(s, '--bg-panel', '#faf8f4'),
    bgHeaderHasFocus: v(s, '--bg-hover', '#f1efe9'),
    bgHeaderHovered: v(s, '--bg-hover', '#f1efe9'),
    borderColor: v(s, '--border', '#eceae3'),
    horizontalBorderColor: v(s, '--border', '#eceae3'),
    headerFontStyle: '600 12px',
    baseFontStyle: '12.5px',
    fontFamily: v(s, '--font', "'IBM Plex Sans', system-ui, sans-serif"),
    cellHorizontalPadding: 8,
    headerIconSize: 16,
  };
}

/** Canvas cells cannot resolve CSS variables themselves, so snapshot the small
 * semantic palette they need alongside the main grid theme. */
export function readGridCellPalette(): GridCellPalette {
  const s = tokens();
  const error = v(s, '--red', '#b3392f');
  const empty = v(s, '--text-light', '#8f8879');
  return {
    error: { textDark: error, textLight: error },
    empty: { textDark: empty, textLight: empty },
    pending: {
      background: v(s, '--ai-bg', '#eee7fb'),
      highlight: v(s, '--ai', '#7c5ce6'),
    },
  };
}

/** AI/generated-column tint override, derived from the CSS token palette.
 *  fgIconHeader uses the foreground token for solid surfaces, with white as
 *  the documented light/dark fallback while older themes lack that token.
 *
 *  ONE derived object covers both header treatments Glide paints for an
 *  AI-generated column: this is passed as each AI column's own
 *  `themeOverride` (SheetGrid.tsx's `columns` memo) AND, unchanged, as the
 *  op-group banner's `overrideTheme` (getGroupDetails in
 *  useGridHeaderHandlers) — previously the banner had its OWN hardcoded hex
 *  triplet (#f4f0ff/#ebe3fb/#563d9a), which (a) never re-derived on a dark-
 *  mode flip, rendering a light-lavender/near-white banner over a dark grid,
 *  and (b) diverged from a column header that WASN'T part of a group (no
 *  overrideTheme leak at all, so it stayed correctly dark) — the "one header
 *  light, its neighbor dark" symptom. Reusing this single object for both
 *  removes the second definition instead of just recoloring it. */
export function readAiColumnTheme(): Partial<Theme> {
  const s = tokens();
  const bg = v(s, '--ai-bg', '#eee7fb');
  const accent = v(s, '--ai', '#7c5ce6');
  return {
    bgCell: bg,
    bgCellMedium: bg,
    accentLight: bg,
    bgIconHeader: accent,
    fgIconHeader: v(s, '--text-on-solid', '#ffffff'),
    textHeader: v(s, '--text', '#211f1b'),
    bgHeader: bg,
    bgHeaderHovered: mixHex(bg, accent, 0.18),
    textGroupHeader: v(s, '--text-mid', '#57534a'),
  };
}
