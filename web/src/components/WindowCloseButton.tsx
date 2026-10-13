import { X } from 'lucide-react';
import type { MouseEventHandler } from 'react';

import styles from './WindowCloseButton.module.css';

export interface WindowCloseButtonProps {
  label: string;
  onClick: MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  className?: string;
}

/** The common close affordance for dialogs, popups, and window-like panels. */
export function WindowCloseButton({
  label,
  onClick,
  disabled = false,
  className,
}: WindowCloseButtonProps) {
  return (
    <button
      type="button"
      className={[styles.button, className].filter(Boolean).join(' ')}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
    >
      <X size={20} strokeWidth={2} aria-hidden />
    </button>
  );
}
