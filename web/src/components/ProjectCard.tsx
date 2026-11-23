import type { ReactNode } from 'react';
import type { ProjectCover } from './projectCover';

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
