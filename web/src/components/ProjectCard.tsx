import type { CSSProperties, ReactNode } from 'react';

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

/** The cover and body of a project card, shared by the Home grid and the
 *  New project preview. */
export function ProjectCardFace({
  cover,
  title,
  titleTestId,
  placeholderTitle = false,
  titlePrefix,
  titleSuffix,
  meta,
  chip,
  servicesLabel,
  servicesTestId,
}: {
  cover: ProjectCover;
  title: string;
  titleTestId?: string;
  placeholderTitle?: boolean;
  titlePrefix?: ReactNode;
  titleSuffix?: ReactNode;
  meta: ReactNode;
  chip?: ReactNode;
  servicesLabel: string | null;
  servicesTestId?: string;
}) {
  return (
    <>
      <span className="home-card-cover" style={cover.style} data-neutral={cover.style.background === undefined}>
        <span className="home-card-glyph">{cover.glyph}</span>
        {chip}
        {servicesLabel && (
          <span className="home-card-services" data-testid={servicesTestId}>{servicesLabel}</span>
        )}
      </span>
      <span className="home-card-body">
        <span className="home-card-titlerow">
          {titlePrefix}
          <span
            className="home-card-title"
            data-placeholder={placeholderTitle || undefined}
            data-testid={titleTestId}
          >
            {title}
          </span>
          {titleSuffix}
        </span>
        {meta}
      </span>
    </>
  );
}
