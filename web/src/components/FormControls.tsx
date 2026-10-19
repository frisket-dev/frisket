import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes, type LabelHTMLAttributes, type ReactNode } from 'react';
import { Search } from 'lucide-react';

type Appearance = { density?: 'compact' | 'standard'; fullWidth?: boolean };
const classes = (...values: (string | undefined)[]) => [...new Set(values.filter(Boolean).join(' ').split(/\s+/))].join(' ');

/** A label and its control, with the same spacing in settings, actions and imports. */
export function FormField({ label, hint, children, className, layout = 'stacked', ...props }: LabelHTMLAttributes<HTMLLabelElement> & {
  label: ReactNode;
  hint?: ReactNode;
  layout?: 'stacked' | 'inline';
}) {
  return <label {...props} data-field-layout={layout} className={classes('form-field', className)}>
    <span className="form-field-label">{label}</span>
    {children}
    {hint && <span className="form-hint">{hint}</span>}
  </label>;
}

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

export const SearchInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function SearchInput(
  { className, ...props }, ref,
) {
  return <div className="search-field"><Search size={14} aria-hidden />
    <TextInput type="search" {...props} ref={ref} className={className} />
  </div>;
});
