import type { TableImportDestinationConfig, TableImportDestinationSnapshot, TableImportColumn } from '../../../api/tableImports';
import { FormField } from '../../FormControls';
import { PanelSelect } from '../../PanelSelect';
import { SegmentedToggle } from '../../PanelPrimitives';

type ExistingDestination = Extract<TableImportDestinationConfig, { kind: 'existing' }>;

export function KeyedUpdateControls({ destination, snapshot, columns, busy, onChange }: {
  destination: ExistingDestination;
  snapshot: TableImportDestinationSnapshot;
  columns: TableImportColumn[];
  busy: boolean;
  onChange(destination: ExistingDestination): void;
}) {
  const update = destination.update;
  const eligible = columns.filter((column) => column.included && snapshot.mappings.some(
    (mapping) => mapping.source_index === column.source_index && mapping.target.kind === 'existing',
  ));
  const relationships = snapshot.update_relationships ?? [];
  const relationshipKeys = (keyColumns: number[]) => keyColumns.map((id) => snapshot.mappings.find(
    (mapping) => mapping.target.kind === 'existing' && mapping.target.column_id === id,
  )?.source_index);
  const change = (patch: Partial<NonNullable<ExistingDestination['update']>>) => {
    if (update) onChange({ ...destination, update: { ...update, ...patch } });
  };
  return <div className="form-stack">
    <SegmentedToggle ariaLabel="Import behavior" fullWidth={false} disabled={busy}
      value={update ? 'update' : 'append'}
      options={[{ value: 'append', label: 'Add rows' }, { value: 'update', label: 'Update matching rows' }]}
      onValueChange={(mode) => onChange({ ...destination, update: mode === 'update'
        ? { key_source_indices: [], policy: 'keep_changes' } : null })} />
    {update && <>
      {relationships.length > 0 && <FormField label="Update source">
        <PanelSelect aria-label="Update source" value={update.relationship_id ?? ''} disabled={busy}
          onChange={(event) => {
            const relationship = relationships.find((item) => item.relationship_id === event.currentTarget.value);
            change({ relationship_id: relationship?.relationship_id ?? null,
              key_source_indices: relationship ? relationshipKeys(relationship.key_column_ids).filter((index): index is number => index != null) : [],
            });
          }}>
          <option value="">New update source</option>
          {relationships.map((relationship) => <option key={relationship.relationship_id} value={relationship.relationship_id}
            disabled={relationshipKeys(relationship.key_column_ids).some((index) => index == null)}>{relationship.label}</option>)}
        </PanelSelect>
      </FormField>}
      <div role="group" aria-label="Match rows using" className="form-stack">
        <span className="form-field-label">Match rows using</span>
        {eligible.map((column) => <label className="form-check" key={column.source_index}>
          <input type="checkbox" disabled={busy || !!update.relationship_id} checked={update.key_source_indices.includes(column.source_index)}
            onChange={(event) => change({ key_source_indices: event.currentTarget.checked
              ? [...update.key_source_indices, column.source_index]
              : update.key_source_indices.filter((index) => index !== column.source_index) })} />
          {column.name}
        </label>)}
      </div>
      {update.key_source_indices.length === 0 && <p className="form-hint">Choose the columns that identify each row.</p>}
      {update.key_source_indices.length >= columns.filter((column) => column.included && column.target_type !== 'omit').length && <p className="form-hint">Keep at least one other column to update.</p>}
      <FormField label="When imported values change">
        <PanelSelect aria-label="When imported values change" disabled={busy} value={update.policy ?? 'keep_changes'}
          onChange={(event) => change({ policy: event.currentTarget.value as 'keep_changes' | 'incoming_wins' })}>
          <option value="keep_changes">Keep my changes</option>
          <option value="incoming_wins">Use imported values</option>
        </PanelSelect>
      </FormField>
      <label className="form-check"><input type="checkbox" disabled={busy} checked={update.keep_existing_on_blank ?? false}
        onChange={(event) => change({ keep_existing_on_blank: event.currentTarget.checked })} />Keep existing values when imported cells are blank</label>
      <p className="form-hint">Only matching rows are updated. New keys are skipped.</p>
    </>}
  </div>;
}
