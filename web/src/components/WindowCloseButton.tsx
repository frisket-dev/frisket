import { X } from 'lucide-react';
import { forwardRef } from 'react';
import { IconButton, type IconButtonProps } from './IconButton';

export type WindowCloseButtonProps = Omit<IconButtonProps, 'icon'>;

/** The common close affordance for dialogs, popups, and window-like panels. */
export const WindowCloseButton = forwardRef<HTMLButtonElement, WindowCloseButtonProps>(function WindowCloseButton(
  { size = 'standard', ...props }, ref,
) {
  return <IconButton {...props} ref={ref} icon={X} size={size} />;
});
