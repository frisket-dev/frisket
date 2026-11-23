import type { CSSProperties } from 'react';

// A deterministic hue and a faded glyph, so a card is recognizable at a
// glance. The key is the project's cover seed, or its id when it has none:
// projects created without a seed keep the cover their id always gave them.
const COVER_GLYPHS = ['◈', '⛃', '§', '◉', '¶', '▦', '◔', '⧉'];
const SEED_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';

function hashString(value: string): number {
  let h = 0;
  for (let i = 0; i < value.length; i += 1) {
    h = (h << 5) - h + value.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h);
}

export interface ProjectCover {
  style: CSSProperties;
  glyph: string;
}

function coverForKey(key: string): ProjectCover {
  const hash = hashString(key);
  const hue = hash % 360;
  const hue2 = (hue + 40) % 360;
  return {
    style: { background: `linear-gradient(135deg, hsl(${hue} 42% 88%), hsl(${hue2} 46% 80%))` },
    glyph: COVER_GLYPHS[hash % COVER_GLYPHS.length],
  };
}

export function projectCover(project: { id: string; cover_seed?: string | null }): ProjectCover {
  return coverForKey(project.cover_seed ?? project.id);
}

/** The cover a seed gives a project that does not exist yet. */
export function seedCover(seed: string): ProjectCover {
  return coverForKey(seed);
}

/** A fresh seed whose cover differs from `previous`'s, so "Different cover"
 *  always changes what the person sees. */
export function newCoverSeed(previous: string | null = null): string {
  const before = previous === null ? null : coverForKey(previous);
  for (;;) {
    const bytes = new Uint8Array(10);
    crypto.getRandomValues(bytes);
    const seed = Array.from(bytes, (value) => SEED_ALPHABET[value % SEED_ALPHABET.length]).join('');
    const cover = coverForKey(seed);
    if (before === null || cover.glyph !== before.glyph || cover.style.background !== before.style.background) {
      return seed;
    }
  }
}
