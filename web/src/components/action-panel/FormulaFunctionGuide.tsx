import { useId, useRef, useState, type ReactNode } from 'react';
import { Button } from '../Button';
import { TextInput } from '../FormControls';
import { PinnedReferencePanel } from '../PinnedReferencePanel';

// A few starting points, not a parallel engine function catalog. The linked
// upstream reference owns the complete function documentation.
const EXAMPLES = [
  { group: 'Numbers', signature: 'ROUND(number, digits)', description: 'Round a number to a chosen precision.', example: '=ROUND({{amount}}, 2)' },
  { group: 'Numbers', signature: 'SUM(number, …)', description: 'Add values from the current row.', example: '=SUM({{amount}}, {{tax}})' },
  { group: 'Conditions', signature: 'IF(condition, when_true, when_false)', description: 'Choose between two values. Use a blank to keep numeric results one type.', example: '=IF({{amount}}>0, {{amount}}, "")' },
  { group: 'Conditions', signature: 'IFERROR(value, fallback)', description: 'Use a fallback when a calculation returns an error.', example: '=IFERROR({{amount}} / {{quantity}}, "")' },
  { group: 'Text', signature: 'TRIM(text)', description: 'Remove extra spaces from text.', example: '=TRIM({{first name}}) & " " & TRIM({{last name}})' },
  { group: 'Text', signature: 'LOWER(text)', description: 'Convert text to lowercase.', example: '=LOWER({{email}})' },
  { group: 'Text', signature: 'LEN(text)', description: 'Count the characters in text.', example: '=LEN({{headline}})' },
  { group: 'Dates', signature: 'YEAR(date)', description: 'Get the year from a date value.', example: '=YEAR({{date}})' },
  { group: 'Dates', signature: 'TODAY()', description: 'Return today’s date, calculated once for this run.', example: '=TODAY()' },
  { group: 'Random', signature: 'RAND()', description: 'Draw a number from 0 up to 1. Preview and Run make separate draws.', example: '=RAND()' },
];

export function FormulaFunctionGuide({ label, fieldId, children }: {
  label: string;
  fieldId: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const anchorRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const normalized = query.trim().toLowerCase();
  const examples = EXAMPLES.filter((item) => `${item.group} ${item.signature} ${item.description} ${item.example}`
    .toLowerCase().includes(normalized));
  const groups = [...new Set(examples.map((item) => item.group))];
  return <div ref={anchorRef}>
    <div className="source-input-head">
      <label className="form-label" htmlFor={fieldId}>{label}</label>
      <Button ref={triggerRef} type="button" variant="text" aria-expanded={open} aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}>Functions &amp; examples</Button>
    </div>
    {open && <PinnedReferencePanel id={panelId} title="Common examples" anchorRef={anchorRef} sourceRef={triggerRef}
      onClose={() => { setOpen(false); triggerRef.current?.focus(); }}>
      <p><a href="https://www.formualizer.dev/docs/reference/functions" target="_blank" rel="noreferrer">
        Full Formualizer function reference ↗</a></p>
      <TextInput aria-label="Filter formula examples" placeholder="Find a function or example…"
        value={query} onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => { if (event.key === 'Enter') event.preventDefault(); }} />
      {groups.map((group) => <section key={group} aria-label={group}>
        <h4>{group}</h4>
        <dl>{examples.filter((item) => item.group === group).map((item) => <div key={item.signature}>
          <dt><code>{item.signature}</code></dt>
          <dd>{item.description}<code>{item.example}</code></dd>
        </div>)}</dl>
      </section>)}
      {examples.length === 0 && <p className="form-hint">No matching examples. Try another term or open the full reference.</p>}
    </PinnedReferencePanel>}
    {children}
  </div>;
}
