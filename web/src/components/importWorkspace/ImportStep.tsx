import type { HTMLAttributes } from 'react';
import './ImportWorkspaceShared.css';

/** A step owns one scrolling body and a non-scrolling footer. */
export function ImportStep({ className, ...props }: HTMLAttributes<HTMLElement>) {
  return <section {...props} className={['import-step', className].filter(Boolean).join(' ')} />;
}

export function ImportStepBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div {...props} className={['import-step-body', className].filter(Boolean).join(' ')} />;
}
