import type { ReactNode } from 'react';

import './ImportWorkspaceShared.css';

export function ImportStepFooter({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <footer className={['import-step-footer', className].filter(Boolean).join(' ')}>
      {children}
    </footer>
  );
}
