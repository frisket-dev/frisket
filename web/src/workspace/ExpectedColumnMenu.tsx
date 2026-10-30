import { useState } from 'react';
import type { EvaluationLink, EvaluationRule, ExpectedProjection } from '../api/evaluation';
import { Button } from '../components/Button';
import { Modal } from '../components/Modal';
import { PanelSelect } from '../components/PanelSelect';

export function ExpectedColumnMenu({ column, link, onClose, onChange, onRuleChange, onEditJoin, onCompare }: {
  column: ExpectedProjection['columns'][number]; link: EvaluationLink;
  onClose(): void; onChange(link: EvaluationLink): Promise<void>; onRuleChange(rule: EvaluationRule): Promise<void>; onEditJoin(): void; onCompare(): void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const change = async (next: EvaluationLink) => {
    setBusy(true); setError('');
    try { await onChange(next); onClose(); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not update expected answers.'); } finally { setBusy(false); }
  };
  return <Modal title="Expected column" onClose={onClose} busy={busy} closeLabel="Close">
    <div className="modal-body">
      <Button type="button" onClick={onCompare}>Compare on these rows</Button>
      <label>Match rule <PanelSelect ariaLabel="Match rule" value={column.rule} options={[
        { value: 'exact', label: 'Exact' }, { value: 'ignore_case', label: 'Ignore case' },
        { value: 'number', label: 'Same number' }, { value: 'date', label: 'Same date' },
        { value: 'set', label: 'Same items' }, { value: 'hand', label: 'Score by hand' },
      ]} onValueChange={(value) => {
        setBusy(true); setError('');
        void onRuleChange(value as EvaluationRule).then(onClose, (reason: unknown) => setError(reason instanceof Error ? reason.message : 'Could not update match rule.')).finally(() => setBusy(false));
      }} /></label>
      <Button type="button" onClick={() => void change({ ...link, fields: link.fields.map(field => field.expected_column_id === column.expected_column_id ? { ...field, hidden: true } : field) })}>Hide this column</Button>
      <Button type="button" onClick={() => void change({ ...link, hidden: true })}>Hide all expected</Button>
      <Button type="button" onClick={onEditJoin}>Edit join…</Button>
      {error && <p role="alert">{error}</p>}
    </div>
  </Modal>;
}
