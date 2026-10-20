import { forwardRef, type HTMLAttributes } from 'react';
import './PanelLayout.css';

/** A panel owns its scroll region; headers and footers remain outside it. */
export function PanelFrame({
  as: Tag = 'section', fill = false, className, ...props
}: HTMLAttributes<HTMLElement> & { as?: 'section' | 'aside'; fill?: boolean }) {
  return <Tag {...props} className={['panel-frame', className].filter(Boolean).join(' ')} data-fill={fill || undefined} />;
}

export const PanelBody = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement> & {
  padding?: 'standard' | 'compact' | 'none';
}>(function PanelBody({ padding = 'standard', className, ...props }, ref) {
  return <div {...props} ref={ref} className={['panel-body', className].filter(Boolean).join(' ')} data-padding={padding} />;
});

export function PanelFooter({
  align = 'start', className, ...props
}: HTMLAttributes<HTMLElement> & { align?: 'start' | 'end' }) {
  return <footer {...props} className={['panel-footer', className].filter(Boolean).join(' ')} data-align={align} />;
}
