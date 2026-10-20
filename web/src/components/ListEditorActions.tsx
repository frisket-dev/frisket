import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';
import { Button } from './Button';

/** Shared save/cancel controls; drafts, validation, and mutations stay with the list. */
export function ListEditorActions({
  saveLabel, onSave, testId, icon: Icon, busy = false, disabled = false,
  saveDisabled = false, onCancel, cancelTestId,
}: {
  saveLabel: ReactNode;
  onSave(): void;
  testId?: string;
  icon?: LucideIcon;
  busy?: boolean;
  disabled?: boolean;
  saveDisabled?: boolean;
  onCancel?: () => void;
  cancelTestId?: string;
}) {
  return (
    <>
      <Button type="button" className="btn-primary" data-testid={testId}
        aria-busy={busy} disabled={busy || disabled || saveDisabled} onClick={onSave}>
        {Icon && <Icon size={14} aria-hidden />} {saveLabel}
      </Button>
      {onCancel && (
        <Button type="button" data-testid={cancelTestId} disabled={busy || disabled} onClick={onCancel}>
          Cancel
        </Button>
      )}
    </>
  );
}
