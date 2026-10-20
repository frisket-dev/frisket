import { TextArea } from '../FormControls';
import type { ReactNode } from 'react';

import {
  handleAutoResizeTextareaInput,
  resizeTextareaToContent,
} from './formControlHelpers';

export interface ActionPromptFieldProps {
  id?: string;
  testId?: string;
  label: ReactNode;
  value: string;
  rows: number;
  onChange(value: string): void;
  hint?: ReactNode;
  hintTestId?: string;
}

export function ActionPromptField({
  id = 'action-prompt',
  testId = 'action-prompt',
  label,
  value,
  rows,
  onChange,
  hint,
  hintTestId,
}: ActionPromptFieldProps) {
  return (
    <>
      <label className="form-label" htmlFor={id}>{label}</label>
      <TextArea
        id={id}
        className="form-textarea-autogrow"
        ref={resizeTextareaToContent}
        rows={rows}
        value={value}
        data-testid={testId}
        onChange={(event) => onChange(event.target.value)}
        onInput={handleAutoResizeTextareaInput}
      />
      {hint !== undefined && hint !== null && (
        <p className="form-hint" data-testid={hintTestId}>{hint}</p>
      )}
    </>
  );
}
