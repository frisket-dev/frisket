import { useState } from 'react';
import type { ViewAlertConditions as Conditions } from '../api/types';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useSelector } from '../bind/useSelector';
import { FormField, TextInput } from './FormControls';
import { PanelSelect } from './PanelSelect';

export const DEFAULT_VIEW_ALERTS: Conditions = { kind: 'conditions', entered: true, exited: false, count_changed: false, fields: [] };

/** Shared View alert conditions; selecting these does not authorize model preparation. */
export function ViewAlertConditions({ value, onChange, sheetId, disabled = false }: {
  value: Conditions; onChange(value: Conditions): void; sheetId?: number | null; disabled?: boolean;
}) {
  const projectData = useProjectDataResource();
  const sheets = useSelector(projectData.store, (state) => state.sheets);
  const [chooseFields, setChooseFields] = useState(value.fields.length > 0);
  const columns = sheets.filter((sheet) => sheetId == null || Number(sheet.id) === sheetId)
    .flatMap((sheet) => sheet.columns.map((column) => ({ id: Number(column.id), label: sheetId == null ? `${sheet.name}: ${column.name}` : column.name })));
  const change = (patch: Partial<Conditions>) => onChange({ ...value, ...patch });
  return <div className="form-stack">
    <label className="form-check"><input type="checkbox" checked={value.entered} disabled={disabled} onChange={(event) => change({ entered: event.currentTarget.checked })} />New matches</label>
    <label className="form-check"><input type="checkbox" checked={chooseFields} disabled={disabled} onChange={(event) => {
      setChooseFields(event.currentTarget.checked); if (!event.currentTarget.checked) change({ fields: [] });
    }} />Changes to selected columns</label>
    {chooseFields && <div role="group" aria-label="Columns to monitor" className="form-stack">
      {columns.map((column) => <label key={column.id} className="form-check"><input type="checkbox" disabled={disabled}
        checked={value.fields.some((field) => field.column_id === column.id)} onChange={(event) => change({ fields: event.currentTarget.checked
          ? [...value.fields, { column_id: column.id }] : value.fields.filter((field) => field.column_id !== column.id) })} />{column.label}</label>)}
      {value.fields.length === 0 && <span className="form-hint">Choose columns to monitor.</span>}
    </div>}
    <label className="form-check"><input type="checkbox" checked={value.exited} disabled={disabled} onChange={(event) => change({ exited: event.currentTarget.checked })} />Rows leaving this view</label>
    <details><summary>Count alerts</summary><div className="form-stack">
      <label className="form-check"><input type="checkbox" checked={value.count_changed} disabled={disabled} onChange={(event) => change({ count_changed: event.currentTarget.checked })} />Matching row count changes</label>
      <FormField label="Count threshold"><TextInput type="number" min={1} disabled={disabled} value={typeof value.threshold?.threshold === 'number' ? value.threshold.threshold : ''}
        placeholder="None" onChange={(event) => change({ threshold: event.currentTarget.value ? { kind: 'threshold_crossed', metric: 'matched_rows', threshold: Number(event.currentTarget.value), direction: value.threshold?.direction ?? 'at_or_above' } : undefined })} /></FormField>
      {value.threshold && <PanelSelect aria-label="Threshold direction" disabled={disabled} value={String(value.threshold.direction ?? 'at_or_above')}
        onChange={(event) => change({ threshold: { ...value.threshold, direction: event.currentTarget.value } })}>
        <option value="at_or_above">Reaches or exceeds threshold</option><option value="below">Falls below threshold</option><option value="any">Crosses in either direction</option>
      </PanelSelect>}
    </div></details>
  </div>;
}
