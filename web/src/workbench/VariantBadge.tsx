import type { CSSProperties, HTMLAttributes } from 'react';

import './VariantBadge.css';
import { VARIANT_BADGE_PRESENTATION } from './variantBadgePresentation';

export interface VariantBadgeProps extends Omit<HTMLAttributes<HTMLSpanElement>, 'children'> {
  label: string;
}

/** Compact boxed identity shared by comparison chips and canvas headers. */
export function VariantBadge({ label, className, style, ...props }: VariantBadgeProps) {
  const { size, radius, fontSize, fontWeight } = VARIANT_BADGE_PRESENTATION;
  const badgeStyle = {
    '--variant-badge-size': `${size}px`,
    '--variant-badge-radius': `${radius}px`,
    '--variant-badge-font-size': `${fontSize}px`,
    '--variant-badge-font-weight': String(fontWeight),
    ...style,
  } as CSSProperties;
  return (
    <span
      {...props}
      className={['variant-badge', className].filter(Boolean).join(' ')}
      data-variant-key={label}
      style={badgeStyle}
    >
      {label}
    </span>
  );
}
