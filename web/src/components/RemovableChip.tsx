import type { MouseEventHandler, ReactNode } from 'react';
import { X } from 'lucide-react';
import { IconButton } from './IconButton';
import './RemovableChip.css';

export function RemovableChip({ children, onRemove, removeLabel, disabled, tone = 'accent', className, testId }: {
  children: ReactNode;
  onRemove: MouseEventHandler<HTMLButtonElement>;
  removeLabel: string;
  disabled?: boolean;
  tone?: 'accent' | 'ai' | 'neutral';
  className?: string;
  testId?: string;
}) {
  return (
    <span className={['removable-chip', className].filter(Boolean).join(' ')} data-tone={tone} data-testid={testId}>
      {children}
      <IconButton icon={X} size="inline" className="removable-chip-remove" label={removeLabel} disabled={disabled} onClick={onRemove} />
    </span>
  );
}
