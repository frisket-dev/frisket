import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from 'react';

type Appearance = { density?: 'compact' | 'standard'; fullWidth?: boolean };
const classes = (...values: (string | undefined)[]) => [...new Set(values.filter(Boolean).join(' ').split(/\s+/))].join(' ');

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & Appearance>(function TextInput(
  { className, density = 'standard', fullWidth = true, ...props }, ref,
) {
  return <input {...props} ref={ref} className={classes('form-input', className)} data-control-density={density} data-control-full-width={fullWidth} />;
});

export const TextArea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement> & Appearance>(function TextArea(
  { className, density = 'standard', fullWidth = true, rows = 3, ...props }, ref,
) {
  return <textarea {...props} ref={ref} rows={rows} className={classes('form-input form-textarea', className)} data-control-density={density} data-control-full-width={fullWidth} />;
});
