import { useEffect, useState } from 'react';
import { freshActionRequestKey } from '../../api/open';
import type { ColumnDef, ReviewRun } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { ConfirmDialog } from '../Modal';
import { OutputNameCombobox } from '../action-panel/TargetSaveToControl';
import { dedupeDefaultColumnName, existingColumnByName } from '../action-panel/formControlHelpers';
import { useAction } from '../../hooks/useAction';

/** Saved logical output bindings come from the run, never today's action schema. */
export function ReapplyResultsDialog({ run, onClose, onApplied }: {
  run: ReviewRun; onClose(): void; onApplied(): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const [columns, setColumns] = useState<ColumnDef[] | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const { busy, error, setError, run: runAction } = useAction();
  const outputs = run.outputs ?? [];
  useEffect(() => {
    let active = true;
    void projectApi.listSheets().then((sheets) => {
      if (!active) return;
      const sheet = sheets.find((item) => item.id === run.sheetId);
      if (!sheet) throw new Error('The original sheet is unavailable.');
      const reserved = [...sheet.columns];
      const initial: Record<string, string> = {};
      for (const output of run.outputs ?? []) {
        const name = dedupeDefaultColumnName(output.columnName, reserved);
        initial[output.key] = name;
        reserved.push({ id: `new:${output.key}`, name, type: output.columnType });
      }
      setNames(initial);
      setColumns(sheet.columns);
    }).catch((reason) => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [projectApi, run, setError]);
  const values = outputs.map((output) => names[output.key]?.trim() ?? '');
  const valid = columns !== null && outputs.length > 0 && values.every(Boolean)
    && new Set(values.map((name) => name.toLowerCase())).size === outputs.length;
  const replacements = values.filter((name) => existingColumnByName(columns ?? [], name));
  const apply = async () => {
    if (!valid || busy || !run.runId) return;
    await runAction(async () => {
      await projectApi.runAction({ action_id: 'run.reapply',
        idempotency_key: freshActionRequestKey('run.reapply'),
        scope: { kind: 'sheet_rows', sheet_id: Number(run.sheetId) },
        params: { source_run_id: Number(run.runId) },
        output_names: Object.fromEntries(outputs.map((output, i) => [output.key, values[i]])),
        replace_existing: replacements.length > 0,
        expected_output_columns: Object.fromEntries(values.map((name) => [name,
          existingColumnByName(columns ?? [], name)?.id == null ? null
            : Number(existingColumnByName(columns ?? [], name)!.id)])),
      });
      onApplied();
    });
  };
  return <ConfirmDialog title="Reapply saved results?" confirmLabel="Reapply results"
    onConfirm={() => { void apply(); }} onCancel={onClose} busy={busy} blocked={!valid}
    testId="reapply-results-confirm">
    <p className="modal-body-text">Copy saved results into these columns. This is undoable and does not call a provider or incur another charge.</p>
    {columns === null && !error && <p role="status">Loading destinations…</p>}
    {columns !== null && <fieldset className="locked-param-fields" disabled={busy}>
      {outputs.map((output) => <OutputNameCombobox key={output.key}
        label={output.columnName} inputTestId={`reapply-output-${output.key}`}
        columns={columns.filter((column) => column.generationManaged === true
          && column.ai !== undefined && column.type === output.columnType)}
        value={names[output.key] ?? ''}
        onChange={(value) => setNames((current) => ({ ...current, [output.key]: value }))} />)}
    </fieldset>}
    {columns !== null && outputs.length > 0 && !valid && <p className="form-hint">Give every output a different, nonempty column name.</p>}
    {replacements.length > 0 && <p className="form-hint" role="status">Reapplying will replace the current values in {replacements.join(', ')}.</p>}
    {columns !== null && !outputs.length && <p role="alert">This execution has no saved output contract.</p>}
    {error && <p role="alert" className="panel-error">{error}</p>}
  </ConfirmDialog>;
}
