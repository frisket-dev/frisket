import { useId, useRef, useState, type ReactNode } from 'react';
import { BookOpen } from 'lucide-react';
import { Button } from '../Button';
import { PinnedReferencePanel } from '../PinnedReferencePanel';

// A few starting points, not a parallel engine function catalog. The linked
// upstream reference owns the complete function documentation.
const EXAMPLE_GROUPS = [
  { title: 'Text', reference: 'text', linkLabel: 'More text functions', examples: [
    { formula: '=TRIM({{name}})', purpose: 'Remove extra spaces from a name.' },
    { formula: '=CLEAN({{address}})', purpose: 'Remove nonprinting characters.' },
    { formula: '=SUBSTITUTE({{amount}}, "$", "")', purpose: 'Remove dollar signs from text.' },
    { formula: '=LEFT({{postcode}}, 3)', purpose: 'Get the first three characters.' },
    { formula: '=RIGHT({{record_id}}, 4)', purpose: 'Get the last four characters.' },
    { formula: '=MID({{record_id}}, 4, 3)', purpose: 'Extract three characters from position four.' },
    { formula: '=LEN({{headline}})', purpose: 'Count headline characters.' },
    { formula: '=LOWER({{email}})', purpose: 'Convert an email to lowercase.' },
    { formula: '=UPPER({{state}})', purpose: 'Capitalize a state abbreviation.' },
    { formula: '=PROPER({{name}})', purpose: 'Convert a name to title case.' },
    { formula: '={{first name}} & " " & {{last name}}', purpose: 'Combine first and last names.' },
    { formula: '=TEXT({{amount}}, "0.00")', purpose: 'Format an amount as text with two decimals.' },
  ] },
  { title: 'Numbers', reference: 'math', linkLabel: 'More math functions', examples: [
    { formula: '=ROUND({{amount}}, 2)', purpose: 'Round to two decimal places.' },
    { formula: '=ABS({{change}})', purpose: 'Get the size of a change.' },
    { formula: '=ROUNDUP({{amount}}, 2)', purpose: 'Round away from zero.' },
    { formula: '=ROUNDDOWN({{amount}}, 2)', purpose: 'Round toward zero.' },
    { formula: '=MOD({{quantity}}, 12)', purpose: 'Get the remainder after division by 12.' },
    { formula: '=IFERROR({{count}} / {{total}} * 100, "")', purpose: 'Calculate a percentage.' },
    { formula: '=RAND()', purpose: 'Generate a random sampling value.' },
  ] },
  { title: 'Dates', reference: 'datetime', linkLabel: 'More date and time functions', examples: [
    { formula: '=YEAR({{published}})', purpose: 'Get the publication year.' },
    { formula: '=MONTH({{published}})', purpose: 'Get the publication month.' },
    { formula: '=DAY({{published}})', purpose: 'Get the day of the month.' },
    { formula: '=DATE({{year}}, {{month}}, {{day}})', purpose: 'Build a date from separate fields.' },
    { formula: '=DAYS({{closed}}, {{opened}})', purpose: 'Count elapsed days for a case.' },
    { formula: '=TODAY()', purpose: 'Get today’s date.' },
    { formula: '=NOW()', purpose: 'Record the current date and time.' },
  ] },
  { title: 'Conditions', reference: 'logical', linkLabel: 'More logical functions', examples: [
    { formula: '=IF({{amount}}>10000, "Review", "")', purpose: 'Flag payments over 10,000.' },
    { formula: '=IFERROR({{amount}} / {{quantity}}, "")', purpose: 'Return a blank on calculation error.' },
    { formula: '=AND({{amount}}>0, {{confirmed}})', purpose: 'Check for a positive, confirmed payment.' },
    { formula: '=OR({{status}}="Missing", {{status}}="Overdue")', purpose: 'Identify missing or overdue records.' },
    { formula: '=NOT({{verified}})', purpose: 'Identify unverified records.' },
    { formula: '=ISBLANK({{address}})', purpose: 'Find missing addresses.' },
  ] },
];

export function FormulaFunctionGuide({ label, fieldId, children }: {
  label: string;
  fieldId: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  return <div ref={anchorRef}>
    <div className="source-input-head">
      <label className="form-label" htmlFor={fieldId}>{label}</label>
      <Button ref={triggerRef} type="button" variant="link" aria-expanded={open} aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}><BookOpen size={14} aria-hidden />Functions &amp; examples</Button>
    </div>
    {open && <PinnedReferencePanel id={panelId} title="Common examples" anchorRef={anchorRef} sourceRef={triggerRef}
      onClose={() => { setOpen(false); triggerRef.current?.focus(); }}>
      {EXAMPLE_GROUPS.map((group) => <section key={group.title} aria-label={group.title}>
        <h4>{group.title}</h4>
        <table aria-label={`${group.title} formula examples`}>
          <thead><tr><th scope="col">Formula</th><th scope="col">What it does</th></tr></thead>
          <tbody>{group.examples.map((item) => <tr key={item.formula}>
            <td><code>{item.formula}</code></td><td>{item.purpose}</td>
          </tr>)}</tbody>
        </table>
        <p><a href={`https://www.formualizer.dev/docs/reference/functions/${group.reference}`} target="_blank" rel="noreferrer">
          {group.linkLabel} ↗</a></p>
      </section>)}
    </PinnedReferencePanel>}
    {children}
  </div>;
}
