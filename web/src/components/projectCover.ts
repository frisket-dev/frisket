import type { CSSProperties } from 'react';

// Cover art is derived from the project id: a deterministic hue and a faded
// glyph, so a card is recognizable at a glance without any stored art.
const COVER_GLYPHS = ['◈', '⛃', '§', '◉', '¶', '▦', '◔', '⧉'];

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

/** The cover a project id derives. A project that does not exist yet has no
 *  id, so its cover is the neutral one. */
export function projectCover(projectId: string | null): ProjectCover {
  if (projectId === null) return { style: {}, glyph: COVER_GLYPHS[0] };
  const hash = hashString(projectId);
  const hue = hash % 360;
  const hue2 = (hue + 40) % 360;
  return {
    style: { background: `linear-gradient(135deg, hsl(${hue} 42% 88%), hsl(${hue2} 46% 80%))` },
    glyph: COVER_GLYPHS[hash % COVER_GLYPHS.length],
  };
}
