import { useEffect } from 'react';
import { PanelSelect } from '../PanelSelect';
import { MultiColumnPicker } from '../MultiColumnPicker';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { useJoinColumnPins, useJoinSheets } from './useJoinSheets';

export function SemanticJoinParamsBody(props: GeneratedActionParamsBodyProps<'join.semantic'>) {
  if (!props.sheet) return <p className="form-hint">Choose a source sheet to configure this action.</p>;
  return <SemanticJoinParamsBodyForSheet {...props} sheet={props.sheet} />;
}

function SemanticJoinParamsBodyForSheet({ params, setParams, setEditorProblem, sheet, Field }:
  GeneratedActionParamsBodyProps<'join.semantic'> & {
    sheet: NonNullable<GeneratedActionParamsBodyProps<'join.semantic'>['sheet']>;
  }) {
  const { sheets, loading, error, refresh } = useJoinSheets(sheet);
  const blocked = loading || Boolean(error);
  const primary = sheets.find((item) => item.id === sheet.id) ?? sheet;
  const target = sheets.find((item) => Number(item.id) === params.target?.sheet_id);
  const carry = params.carry ?? [];
  useEffect(() => {
    if (loading || error || params.target?.sheet_id != null) return;
    const right = sheets.find((item) => item.id !== sheet.id);
    if (right) setParams({ ...params, target: { sheet_id: Number(right.id), column: right.columns[0]?.name ?? '' } });
  }, [loading, error, params, setParams, sheets, sheet.id]);
  const missing = useJoinColumnPins(sheets, !loading && !error, [
    { slot: 'source', sheetId: Number(sheet.id), name: params.source ?? '' },
    { slot: 'target', sheetId: params.target?.sheet_id ?? 0, name: params.target?.column ?? '' },
    ...carry.map((name, index) => ({ slot: `carry-${index}`, sheetId: Number(sheet.id), name })),
  ], (names) => setParams({ ...params,
    source: names.get('source') ?? params.source,
    ...(params.target == null ? {} : { target: { ...params.target,
      column: names.get('target') ?? params.target.column } }),
    ...(params.carry == null ? {} : { carry: carry.map((name, index) => names.get(`carry-${index}`) ?? name) }),
  }));
  useEffect(() => {
    setEditorProblem?.(loading ? 'Loading join source sheets…' : error
      ?? (!target ? 'Choose an available target sheet.' : missing.length ? 'Repair missing join sources.' : null));
    return () => setEditorProblem?.(null);
  }, [loading, error, target, missing.length, setEditorProblem]);
  return <div className="action-io-summary" data-testid="semantic-join-form">
    <button type="button" className="btn btn-ghost" data-testid="semantic-join-refresh-sheets"
      onClick={refresh}>{error ? 'Retry' : 'Refresh sheets'}</button>
    {loading && <p className="form-hint" role="status">Loading sheets…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {missing.length > 0 && <p className="form-error" role="alert" data-testid="semantic-join-repair">
      Source columns missing: {missing.join(', ')}. Choose replacements to repair the join.</p>}
    <fieldset className="resolve-drawer-body-fields" disabled={blocked}>
    <label className="field-group"><span className="form-label">Source column</span>
      <PanelSelect testId="field-source" ariaLabel="Source column" value={params.source ?? ''}
        disabled={blocked}
        options={[...(!primary.columns.some((column) => column.name === params.source)
          ? [{ value: params.source ?? '', label: params.source || 'Choose a column' }] : []),
          ...primary.columns.map((column) => ({ value: column.name, label: column.name }))]}
        onValueChange={(source) => setParams({ ...params, source })} />
    </label>
    <label className="field-group"><span className="form-label">Target sheet</span>
      <PanelSelect testId="field-semantic_target_sheet" ariaLabel="Target sheet" value={target?.id ?? ''}
        disabled={blocked} options={[...(!target ? [{ value: '', label: params.target?.sheet_id != null ? 'Saved sheet unavailable' : 'Choose a sheet' }] : []),
          ...sheets.filter((item) => item.id !== sheet.id).map((item) => ({ value: item.id, label: item.name }))]}
        onValueChange={(id) => setParams({ ...params, target: { sheet_id: Number(id), column: '' } })} />
      <span className="form-hint">Match selected source rows against the whole target sheet.</span>
    </label>
    <label className="field-group"><span className="form-label">Target column</span>
      <PanelSelect testId="field-semantic_target_column" ariaLabel="Target column" value={params.target?.column ?? ''}
        disabled={blocked || !target}
        options={[...(!target?.columns.some((item) => item.name === params.target?.column)
          ? [{ value: params.target?.column ?? '', label: params.target?.column || 'Choose a column' }] : []),
          ...(target?.columns ?? []).map((column) => ({ value: column.name, label: column.name }))]}
        onValueChange={(column) => setParams({ ...params,
          target: { sheet_id: params.target?.sheet_id ?? 0, column } })} />
    </label>
    <div className="field-group"><span className="form-label">Carry source columns into matches</span>
      <MultiColumnPicker testId="field-semantic_carry_columns" ariaLabel="Carry source columns"
        value={carry} options={primary.columns.filter((column) => column.name !== params.source)
          .map(({ name, type, ai }) => ({ name, type, ai: Boolean(ai) }))} disabled={blocked}
        onValueChange={(carry) => setParams({ ...params, carry })} />
    </div>
    <Field name="match_threshold" label="Minimum match score" />
    <Field name="confident_threshold" label="Confident match score" />
    </fieldset>
  </div>;
}
