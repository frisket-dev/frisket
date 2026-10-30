import { useEffect, useMemo, useState } from 'react';
import { Button } from '../Button';
import { FormField } from '../FormControls';
import { Modal } from '../Modal';
import { PanelSelect } from '../PanelSelect';
import { StageStepper } from '../StageStepper';

export type ExpectedRule = 'exact' | 'ignore_case' | 'number' | 'date' | 'set' | 'hand';
export interface JoinColumn { id: number; name: string }
export interface JoinSheet { id: number; name: string; columns: JoinColumn[] }
export interface JoinAction { id: string; label: string; targetSheetId: number; outputs: Array<JoinColumn & { logicalKey: string }> }
export interface ExpectedLinkDraft {
  target_sheet_id: number; expected_sheet_id: number; action_id: string;
  keys: Array<{ expected_column_id: number; target_column_id: number | null }>;
  fields: Array<{ expected_column_id: number; output_column_id: number; logical_key: string; rule: ExpectedRule; hidden: boolean; source: 'import' | 'disagreements' }>;
  hidden: boolean;
}
export interface JoinPreview {
  matched: number; not_found: number; ambiguous_expected: number; ambiguous_target: number;
  unmatched_expected_row_ids: number[];
}
interface Props {
  sheets: JoinSheet[]; actions: JoinAction[]; initialActionId?: string; initialLink?: ExpectedLinkDraft;
  initialStep?: 'sheets' | 'keys' | 'columns';
  onPreview(draft: ExpectedLinkDraft): Promise<JoinPreview>;
  onSave(draft: ExpectedLinkDraft): Promise<void>;
  onImport(): void; onClose(): void;
}
const rules: Array<{ value: ExpectedRule; label: string }> = [
  { value: 'exact', label: 'Exact' }, { value: 'ignore_case', label: 'Ignore case' },
  { value: 'number', label: 'Same number' }, { value: 'date', label: 'Same date' },
  { value: 'set', label: 'Same items' }, { value: 'hand', label: 'Score by hand' },
];
const steps = [{ id: 'sheets', label: 'Sheets', testId: 'join-step-sheets' }, { id: 'keys', label: 'Match rows', testId: 'join-step-keys' }, { id: 'columns', label: 'Columns', testId: 'join-step-columns' }];

export function JoinExpectedAnswersDialog({ sheets, actions, initialActionId, initialLink, initialStep = 'sheets', onPreview, onSave, onImport, onClose }: Props) {
  const [step, setStep] = useState(initialStep);
  const [sheetId, setSheetId] = useState(initialLink?.expected_sheet_id ?? 0);
  const [actionId, setActionId] = useState(initialLink?.action_id ?? initialActionId ?? '');
  const [keys, setKeys] = useState(initialLink?.keys ?? [{ expected_column_id: 0, target_column_id: 0 }]);
  const [fields, setFields] = useState(initialLink?.fields ?? []);
  const [preview, setPreview] = useState<JoinPreview | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [showUnmatched, setShowUnmatched] = useState(false);
  const sheet = sheets.find((item) => item.id === sheetId);
  const action = actions.find((item) => item.id === actionId);
  const target = sheets.find((item) => item.id === action?.targetSheetId);
  const keyIds = new Set(keys.map((key) => key.expected_column_id));
  const remaining = sheet?.columns.filter((column) => !keyIds.has(column.id)) ?? [];
  const draft = useMemo<ExpectedLinkDraft | null>(() => action && sheet ? {
    action_id: action.id, target_sheet_id: action.targetSheetId, expected_sheet_id: sheet.id,
    keys, fields, hidden: initialLink?.hidden ?? false,
  } : null, [action, sheet, keys, fields, initialLink]);
  const validKeys = !!draft && keys.length > 0 && keys.every((key) => key.expected_column_id > 0 && (key.target_column_id === null ? !!initialLink?.keys.some((item) => item.target_column_id === null) : key.target_column_id > 0))
    && new Set(keys.map((key) => key.expected_column_id)).size === keys.length
    && new Set(keys.map((key) => key.target_column_id)).size === keys.length;
  // A pending or failed preview cannot authorize advancing, and late results cannot replace newer ones.
  useEffect(() => {
    setPreview(null); setPreviewError('');
    if (!draft || !validKeys) return;
    let active = true;
    onPreview(draft).then((result) => { if (active) setPreview(result); }, (reason: unknown) => { if (active) setPreviewError(reason instanceof Error ? reason.message : 'Could not preview matching rows.'); });
    return () => { active = false; };
  }, [draft, validKeys, onPreview]);
  const canAdvance = !!preview && !preview.ambiguous_expected && !preview.ambiguous_target;
  const mappedOutputIds = fields.map((field) => field.output_column_id);
  const validFields = fields.length > 0 && new Set(mappedOutputIds).size === mappedOutputIds.length;
  function prepareFields() {
    if (!action) return;
    setFields(remaining.flatMap((column) => {
      const old = fields.find((field) => field.expected_column_id === column.id);
      if (old) return [old];
      const output = action.outputs.find((item) => item.name === column.name);
      return output ? [{ expected_column_id: column.id, output_column_id: output.id, logical_key: output.logicalKey, rule: 'exact' as const, hidden: false, source: 'import' as const }] : [];
    }));
  }
  async function save() {
    if (!draft || !canAdvance || !validFields || saving) return;
    setSaving(true); setError('');
    try { await onSave(draft); onClose(); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not join expected answers.'); } finally { setSaving(false); }
  }
  const options = (columns: JoinColumn[]) => [{ value: '', label: 'Choose a column' }, ...columns.map((column) => ({ value: String(column.id), label: column.name }))];
  return <Modal title={initialLink ? 'Edit expected answers join' : 'Join expected answers'} onClose={onClose} busy={saving} testId="join-expected-dialog" closeLabel="Close">
    <StageStepper label="Join expected answers steps" stages={steps} stage={step} />
    <div className="modal-body">
      {step === 'sheets' && <>
        <p>Import known answers as an ordinary sheet, then join them to an action.</p>
        <FormField label="Expected sheet"><PanelSelect ariaLabel="Expected sheet" value={sheetId || ''} onValueChange={(value) => { setSheetId(Number(value)); setKeys([{ expected_column_id: 0, target_column_id: 0 }]); setFields([]); }} options={[{ value: '', label: 'Choose a sheet' }, ...sheets.filter((item) => item.id !== action?.targetSheetId).map((item) => ({ value: String(item.id), label: item.name }))]} /></FormField>
        <Button type="button" onClick={onImport}>Import a sheet…</Button>
        <FormField label="Action to check"><PanelSelect ariaLabel="Action to check" value={actionId} onValueChange={(value) => { setActionId(value); setKeys([{ expected_column_id: 0, target_column_id: 0 }]); setFields([]); }} options={[{ value: '', label: 'Choose an action' }, ...actions.map((item) => ({ value: item.id, label: item.label }))]} /></FormField>
        {target && <p>Target sheet: {target.name}</p>}
      </>}
      {step === 'keys' && <><p>Rows match when all of these are equal.</p>
        {keys.map((key, index) => <div key={index}>
          <PanelSelect ariaLabel={`Expected key ${index + 1}`} value={key.expected_column_id || ''} options={options(sheet?.columns ?? [])} onValueChange={(value) => setKeys(keys.map((item, i) => i === index ? { ...item, expected_column_id: Number(value) } : item))} />
          {' = '}
          <PanelSelect ariaLabel={`Target key ${index + 1}`} value={key.target_column_id === null ? 'row-id' : key.target_column_id || ''} options={[...options(target?.columns ?? []), ...(initialLink?.keys.some((item) => item.target_column_id === null) ? [{ value: 'row-id', label: 'Row ID' }] : [])]} onValueChange={(value) => setKeys(keys.map((item, i) => i === index ? { ...item, target_column_id: value === 'row-id' ? null : Number(value) } : item))} />
          <Button type="button" disabled={keys.length === 1} aria-label={`Remove key ${index + 1}`} onClick={() => setKeys(keys.filter((_, i) => i !== index))}>×</Button>
        </div>)}
        <Button type="button" onClick={() => setKeys([...keys, { expected_column_id: 0, target_column_id: 0 }])}>+ Another column</Button>
        {preview && <p role="status">{preview.matched} matched · {preview.not_found} not found {preview.not_found > 0 && <Button type="button" onClick={() => setShowUnmatched(!showUnmatched)}>{showUnmatched ? 'Hide' : 'Show'}</Button>} · {preview.ambiguous_expected + preview.ambiguous_target} match more than one</p>}
        {showUnmatched && preview && <p>Expected row IDs: {preview.unmatched_expected_row_ids.join(', ')}</p>}
        {previewError && <p role="alert">{previewError}</p>}
      </>}
      {step === 'columns' && <><p>Choose which columns contain expected answers.</p>{remaining.map((column) => {
        const field = fields.find((item) => item.expected_column_id === column.id);
        return <div key={column.id}><span>{column.name} → </span>
          <PanelSelect ariaLabel={`Role for ${column.name}`} value={field ? String(field.output_column_id) : ''} options={[{ value: '', label: "Don't join / Choose an output" }, ...(action?.outputs ?? []).map((output) => ({ value: String(output.id), label: `Expected · ${output.name}` }))]} onValueChange={(value) => {
            const output = action?.outputs.find((item) => item.id === Number(value));
            setFields([...fields.filter((item) => item.expected_column_id !== column.id), ...(output ? [{ expected_column_id: column.id, output_column_id: output.id, logical_key: output.logicalKey, rule: field?.rule ?? 'exact', hidden: field?.hidden ?? false, source: field?.source ?? 'import' as const }] : [])]);
          }} />
          {field && <PanelSelect ariaLabel={`Match rule for ${column.name}`} value={field.rule} options={rules} onValueChange={(value) => setFields(fields.map((item) => item.expected_column_id === column.id ? { ...item, rule: value as ExpectedRule } : item))} />}
        </div>;
      })}{!validFields && fields.length > 0 && <p role="alert">Choose a different output for each expected column.</p>}</>}
      {error && <p role="alert">{error}</p>}
    </div>
    <div className="form-actions"><Button type="button" disabled={saving} onClick={step === 'sheets' ? onClose : () => setStep(step === 'columns' ? 'keys' : 'sheets')}>{step === 'sheets' ? 'Cancel' : 'Back'}</Button>
      {step === 'columns' ? <><span>{fields.length} expected fields · {preview?.matched ?? 0} rows</span><Button type="button" className="btn-primary" disabled={!canAdvance || !validFields || saving} onClick={save}>{saving ? 'Joining…' : 'Join'}</Button></> : <Button type="button" className="btn-primary" disabled={step === 'sheets' ? !sheet || !action || sheet.id === action.targetSheetId || !target : !canAdvance} onClick={() => { if (step === 'sheets') setStep('keys'); else { prepareFields(); setStep('columns'); } }}>Next</Button>}
    </div>
  </Modal>;
}
