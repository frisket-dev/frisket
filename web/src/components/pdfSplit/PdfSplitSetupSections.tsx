import { count } from '../importWorkspace/pdfBatch/model';
import type { Row, SheetMeta } from '../../api/types';
import { Button } from '../Button';
import { TextInput } from '../FormControls';
import { WindowCloseButton } from '../WindowCloseButton';
import { TextSourceBadge } from '../importWorkspace/pdfBatch/PdfOcrPolicy';

export interface SplitSource {
  key: string;
  filename: string;
  page_start: number;
  page_end: number;
  display_start: number;
  source_row_id?: number | null;
  text_category?: 'text' | 'missing' | 'suspicious' | 'unreadable' | null;
}
export type SplitScope = 'selected' | 'filtered' | 'all';
export type SplitOrganization = 'pages' | 'split_documents';
export type SplitDestination = 'new_sheet' | 'replace';

function RadioCard({ name, checked, onSelect, title, detail }: {
  name: string; checked: boolean; onSelect(): void; title: string; detail?: string;
}) {
  return <label className="pdf-split-radio-card" data-selected={checked || undefined}>
    <input type="radio" name={name} checked={checked} onChange={onSelect} />
    <span><strong>{title}</strong>{detail && <small>{detail}</small>}</span>
  </label>;
}

export function SplitSetupSettings({
  sheet, fileColumnId, scopeOptions, scope, onScope, organization, onOrganization,
  destination, onDestination, name, onName, carried, onCarried, sourceCount, pageCount,
}: {
  sheet: SheetMeta; fileColumnId: string; scopeOptions: SplitScope[]; scope: SplitScope;
  onScope(value: SplitScope): void; organization: SplitOrganization; onOrganization(value: SplitOrganization): void;
  destination: SplitDestination; onDestination(value: SplitDestination): void; name: string; onName(value: string): void;
  carried: string[]; onCarried(value: string[]): void; sourceCount: number; pageCount: number;
}) {
  return <section className="pdf-split-settings">
    <h3>PDFs</h3>
    {scopeOptions.map((value) => <RadioCard key={value} name="pdf-split-scope" checked={scope === value} onSelect={() => onScope(value)}
      title={value === 'selected' ? 'Selected rows' : value === 'filtered' ? 'Filtered rows' : `All rows in ${sheet.name}`}
      detail={scope === value ? `${count(sourceCount, 'PDF')} · ${count(pageCount, 'page')}` : undefined} />)}
    <h3>Make</h3>
    <RadioCard name="pdf-split-make" checked={organization === 'pages'} onSelect={() => onOrganization('pages')}
      title="One row per page" detail={count(pageCount, 'row')} />
    <RadioCard name="pdf-split-make" checked={organization === 'split_documents'} onSelect={() => onOrganization('split_documents')}
      title="Split into documents" detail="Find where they start" />
    <h3>Put results in</h3>
    <RadioCard name="pdf-split-destination" checked={destination === 'new_sheet'} onSelect={() => onDestination('new_sheet')} title="New sheet" />
    <TextInput aria-label="New sheet name" value={name} disabled={destination !== 'new_sheet'} onChange={(event) => onName(event.target.value)} />
    <RadioCard name="pdf-split-destination" checked={destination === 'replace'} onSelect={() => onDestination('replace')}
      title="Replace original rows" detail={`${count(sourceCount, 'row')} · can be undone`} />
    <h3>Carry over columns · {carried.length ? `${carried.length} ticked` : 'none'}</h3>
    <p>filename, media, page, source and text are always included. text comes from each page’s text layer or OCR.</p>
    {[false, true].map((derived) => <div key={String(derived)}>
      {derived && <h4>Results for the whole PDF</h4>}
      {sheet.columns.filter((column) => column.id !== fileColumnId && Boolean(column.ai) === derived).map((column) => (
        <label className="form-check" key={column.id}>
          <input type="checkbox" checked={carried.includes(column.id)} onChange={(event) => onCarried(
            event.target.checked ? [...carried, column.id] : carried.filter((id) => id !== column.id),
          )} />
          {column.name}{column.ai ? ` · ${column.ai.actionName}` : ''}
        </label>
      ))}
    </div>)}
  </section>;
}

export function SplitSetupPreview({ sheet, organization, sources, carried, rows, text, textKinds, excluded }: {
  sheet: SheetMeta; organization: SplitOrganization; sources: SplitSource[]; carried: string[];
  rows: Row[]; text: Record<string, string>; textKinds: Record<string, 'native' | 'ocr' | null>; excluded: Array<{ row_id: number; reason: string }>;
}) {
  const pageCount = sources.reduce((sum, source) => sum + source.page_end - source.page_start + 1, 0);
  const preview: Array<{ key: string; filename: string; page: number; rowId: number | null }> = [];
  for (const source of sources) {
    for (let page = source.page_start; page <= source.page_end && preview.length < 9; page += 1) {
      preview.push({ key: `${source.key}:${page}`, filename: source.filename, page, rowId: source.source_row_id ?? null });
    }
    if (preview.length === 9) break;
  }
  return <section className="pdf-split-preview">
    <h3>{organization === 'pages' ? count(pageCount, 'row') : `${count(sources.length, 'PDF')} · ${count(pageCount, 'page')}`}</h3>
    <p>{organization === 'pages' ? `from ${count(sources.length, 'PDF')}` : 'Each file’s first page starts a document'}</p>
    {organization === 'pages' ? <>
      <table><thead><tr><th>filename</th><th>page</th>
        {carried.map((id) => <th key={id}>{sheet.columns.find((column) => column.id === id)?.name}</th>)}<th>text</th>
      </tr></thead><tbody>{preview.map((row) => <tr key={row.key}>
        <td>{row.filename}</td><td>{row.page}</td>
        {carried.map((id) => {
          const sourceRow = rows.find((item) => Number(item.id) === row.rowId);
          const bounded = sourceRow?.boundedValues?.[id];
          const value = bounded ? `${bounded.preview}…` : sourceRow?.cells[id];
          return <td key={id}>{value == null ? '—' : typeof value === 'object' ? JSON.stringify(value).slice(0, 160) : String(value).slice(0, 160)}</td>;
        })}
        <td>{text[row.key] || '—'}</td>
      </tr>)}</tbody></table>
      {pageCount > 9 && <p>+ {pageCount - 9} more rows</p>}
    </> : <table><thead><tr><th>filename</th><th>pages</th><th>page text</th></tr></thead>
      <tbody>{sources.slice(0, 9).map((source) => {
        const key = `${source.key}:${source.page_start}`;
        return <tr key={source.key}><td>{source.filename}</td><td>{source.page_end - source.page_start + 1}</td>
          <td>{source.text_category == null ? 'Checking text' : <TextSourceBadge category={source.text_category} policy="none" textKind={textKinds[key] ?? null} />}
            <span>{text[key]}</span></td></tr>;
      })}</tbody>
    </table>}
    {excluded.slice(0, 9).map((row) => <p key={row.row_id} className="pdf-split-excluded">
      {row.reason === 'not_pdf' ? 'Not a PDF' : row.reason === 'invalid_page' ? 'Unavailable page' : row.reason === 'password_protected' ? 'Password protected' : 'Unreadable PDF'} · stays as it is
    </p>)}
    {excluded.length > 9 && <p>+ {excluded.length - 9} more excluded rows</p>}
  </section>;
}

export function SplitSetupHeader({ title, subtitle, onClose }: { title: string; subtitle: string; onClose(): void }) {
  return <header><div><h2>{title}</h2><span>{subtitle}</span></div>
    <WindowCloseButton label={`Close ${title}`} onClick={onClose} />
  </header>;
}

export function SplitSetupFooter({ onBack, note, primary, disabled, onPrimary }: {
  onBack(): void; note?: string; primary: string; disabled?: boolean; onPrimary(): void;
}) {
  return <footer><Button type="button" onClick={onBack}>Cancel</Button><span>{note}</span>
    <Button type="button" className="btn-primary" disabled={disabled} onClick={onPrimary}>{primary}</Button>
  </footer>;
}
