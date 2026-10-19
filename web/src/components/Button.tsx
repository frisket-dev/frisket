import { forwardRef, type ButtonHTMLAttributes } from 'react';

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement>>(function Button(
  { className, children, ...props }, ref,
) {
  return <button {...props} ref={ref} className={['btn', className].filter(Boolean).join(' ')}>{children}</button>;
});

/** Tab navigation keeps its caller's keyboard/selection controller. */
export const TabButton = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement>>(function TabButton(
  { className, children, ...props }, ref,
) {
  return <button type="button" role="tab" {...props} ref={ref} className={['tab-button', className].filter(Boolean).join(' ')}>{children}</button>;
});
