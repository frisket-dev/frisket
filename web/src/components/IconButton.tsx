import { forwardRef, type ButtonHTMLAttributes } from 'react';
import type { LucideIcon } from 'lucide-react';
import './IconButton.css';

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'aria-label'> {
  label: string;
  icon: LucideIcon;
  size?: 'inline' | 'compact' | 'standard';
}

/** Icon-only controls share a target, glyph size, and interaction states. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon: Icon, size = 'compact', className, type = 'button', ...props }, ref,
) {
  return (
    <button {...props} ref={ref} type={type} aria-label={label} data-icon-size={size}
      className={['icon-button', className].filter(Boolean).join(' ')}>
      <Icon size={size === 'standard' ? 20 : size === 'inline' ? 12 : 16} strokeWidth={2} aria-hidden />
    </button>
  );
});
