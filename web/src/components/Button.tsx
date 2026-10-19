import { forwardRef, type ButtonHTMLAttributes } from 'react';

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'standard' | 'text' }>(function Button(
  { className, children, variant = 'standard', ...props }, ref,
) {
  return <button {...props} ref={ref} data-button-variant={variant} className={['btn', className].filter(Boolean).join(' ')}>{children}</button>;
});

/** Tab navigation keeps its caller's keyboard/selection controller. */
export const TabButton = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { tone?: 'accent' | 'discover' | 'monitor' }>(function TabButton(
  { className, children, tone = 'accent', ...props }, ref,
) {
  return <button type="button" role="tab" {...props} ref={ref} data-tab-tone={tone} className={['tab-button', className].filter(Boolean).join(' ')}>{children}</button>;
});
