import { useId } from 'react';
import { FormField, TextInput } from '../FormControls';
import { PanelSelect } from '../PanelSelect';

import './ImportWorkspaceShared.css';

type ImportDestination =
  | { kind: 'new'; sheet_name: string }
  | { kind: 'existing'; sheet_id: number };

export function ImportDestinationPicker({
  destination, sheets, defaultSheetName, preferredSheetId, busy, onDestination,
}: {
  destination: ImportDestination;
  sheets: readonly { id: number; name: string; rowCount?: number }[];
  defaultSheetName: string;
  preferredSheetId?: number | null;
  busy: boolean;
  onDestination(destination: ImportDestination): void;
}) {
  const name = useId();
  const newSheetName = destination.kind === 'new' ? destination.sheet_name : defaultSheetName;
  const preferredSheet = sheets.find((sheet) => sheet.id === preferredSheetId) ?? sheets[0];
  const existingSheetId = destination.kind === 'existing' ? destination.sheet_id : preferredSheet?.id ?? '';
  return (
    <fieldset className="import-destination-options">
      <legend>Import into</legend>
      <div className={`import-destination-choice${destination.kind === 'new' ? ' active' : ''}`}>
        <label>
          <input type="radio" name={name} checked={destination.kind === 'new'} disabled={busy}
            onChange={() => onDestination({ kind: 'new', sheet_name: newSheetName })} />
          <span><strong>New sheet</strong></span>
        </label>
        <FormField label="Sheet name" className="import-destination-choice-field">
          <TextInput value={newSheetName} disabled={busy || destination.kind !== 'new'}
            onChange={(event) => onDestination({ kind: 'new', sheet_name: event.currentTarget.value })} />
        </FormField>
      </div>
      <div className={`import-destination-choice${destination.kind === 'existing' ? ' active' : ''}`}>
        <label>
          <input type="radio" name={name} checked={destination.kind === 'existing'} disabled={busy || sheets.length === 0}
            onChange={() => { if (preferredSheet) onDestination({ kind: 'existing', sheet_id: preferredSheet.id }); }} />
          <span><strong>Add to an existing sheet</strong></span>
        </label>
        <FormField label="Existing sheet" className="import-destination-choice-field">
          <PanelSelect value={existingSheetId}
            disabled={busy || destination.kind !== 'existing' || sheets.length === 0}
            onValueChange={(value) => onDestination({ kind: 'existing', sheet_id: Number(value) })}>
            {sheets.length === 0 && <option value="">No sheets yet</option>}
            {sheets.map((sheet) => <option key={sheet.id} value={sheet.id}>
              {sheet.name}{sheet.rowCount === undefined ? '' : ` · ${sheet.rowCount.toLocaleString()} ${sheet.rowCount === 1 ? 'row' : 'rows'}`}
            </option>)}
          </PanelSelect>
        </FormField>
      </div>
    </fieldset>
  );
}
