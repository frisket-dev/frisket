import { useEffect } from 'react';
import { ArrowDown, ArrowUp, Copy, Plus, X } from 'lucide-react';
import { PanelSelect } from '../PanelSelect';
import { MultiColumnPicker } from '../MultiColumnPicker';
import { ToggleRow } from '../PanelPrimitives';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { useJoinColumnPins, useJoinSheets } from './useJoinSheets';

export function JoinParamsBody(props: GeneratedActionParamsBodyProps<'derive.join'>) {
  if (!props.sheet) return <p className="form-hint">Choose a source sheet to configure this action.</p>;
  return <JoinParamsBodyForSheet {...props} sheet={props.sheet} />;
}

function JoinParamsBodyForSheet({ params, setParams, setEditorProblem, sheet, Field }:
  GeneratedActionParamsBodyProps<'derive.join'> & {
    sheet: NonNullable<GeneratedActionParamsBodyProps<'derive.join'>['sheet']>;
  }) {
  const { sheets, loading, error, refresh } = useJoinSheets(sheet);
  const blocked = loading || Boolean(error);
  const left = sheets.find((item) => item.id === sheet.id) ?? sheet;
  const right = sheets.find((item) => Number(item.id) === params.right?.sheet_id);
  const keys = params.join_keys ?? [];
  const columns = params.columns;
  useEffect(() => {
    if (loading || error || params.right?.sheet_id != null) return;
    const target = sheets.find((item) => item.id !== sheet.id);
    if (target) setParams({ ...params, right: { sheet_id: Number(target.id) },
      join_keys: keys.length ? keys : [{ left_column: left.columns[0]?.name ?? '',
        right_column: target.columns[0]?.name ?? '' }] });
  }, [loading, error, params, setParams, sheets, sheet.id, left.columns, keys]);
  const missing = useJoinColumnPins(sheets, !loading && !error, [
    ...keys.flatMap((key, index) => [
      { slot: `key-left-${index}`, sheetId: Number(sheet.id), name: key.left_column },
      { slot: `key-right-${index}`, sheetId: params.right?.sheet_id ?? 0, name: key.right_column },
    ]),
    ...(columns ?? []).map((column, index) => ({ slot: `column-${index}`,
      sheetId: column.side === 'left' ? Number(sheet.id) : params.right?.sheet_id ?? 0,
      name: column.column })),
  ], (names) => setParams({ ...params,
    join_keys: keys.map((key, index) => ({
      left_column: names.get(`key-left-${index}`) ?? key.left_column,
      right_column: names.get(`key-right-${index}`) ?? key.right_column,
    })),
    ...(columns == null ? {} : { columns: columns.map((column, index) => ({ ...column,
      column: names.get(`column-${index}`) ?? column.column })) }),
  }));
  const changeProjection = (side: 'left' | 'right', names: string[]) => {
    const retained = (columns ?? []).filter((item) => item.side !== side || names.includes(item.column));
    const additions = names.filter((name) => !retained.some((item) => item.side === side && item.column === name));
    const next = [...retained, ...additions.map((column) => ({ side, column }))];
    setParams({ ...params, columns: next.length ? next : null });
  };
  const move = (index: number, delta: number) => {
    const next = [...(columns ?? [])];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    setParams({ ...params, columns: next });
  };
  useEffect(() => {
    setEditorProblem?.(loading ? 'Loading join source sheets…' : error
      ?? (!right ? 'Choose an available right sheet.' : missing.length ? 'Repair missing join sources.' : null));
    return () => setEditorProblem?.(null);
  }, [loading, error, right, missing.length, setEditorProblem]);
  return <div className="action-io-summary" data-testid="tabular-join-form">
    <button type="button" className="btn btn-ghost" data-testid="tabular-join-refresh-sheets"
      onClick={refresh}>{error ? 'Retry' : 'Refresh sheets'}</button>
    {loading && <p className="form-hint" role="status">Loading sheets…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {missing.length > 0 && <p className="form-error" role="alert" data-testid="tabular-join-repair">
      Source columns missing: {missing.join(', ')}. Choose replacements to repair the join.</p>}
    <fieldset className="resolve-drawer-body-fields" disabled={blocked}>
    <p className="form-hint">Left: {left.name}. The selected rows come from this sheet.</p>
    <label className="field-group"><span className="form-label">Right sheet</span>
      <PanelSelect testId="field-join_right_sheet" ariaLabel="Right sheet"
        value={right?.id ?? ''} disabled={blocked}
        options={[...(!right ? [{ value: '', label: params.right?.sheet_id != null ? 'Saved sheet unavailable' : 'Choose a sheet' }] : []),
          ...sheets.filter((item) => item.id !== sheet.id).map((item) => ({ value: item.id, label: item.name }))]}
        onValueChange={(id) => setParams({ ...params, right: { sheet_id: Number(id) },
          join_keys: keys.map((key) => ({ ...key, right_column: '' })),
          ...(columns == null ? {} : { columns: columns.map((column) => column.side === 'right'
            ? { ...column, column: '' } : column) }) })} />
      <span className="form-hint">The right side includes its whole visible sheet.</span>
    </label>
    <span className="form-label">Key pairs</span>
    <div className="tabular-join-key-pairs" data-testid="join-key-pairs">
      {keys.map((key, index) => <div key={index} className="tabular-join-key-pair-row" data-testid={`join-key-pair-${index}`}>
        {(['left', 'right'] as const).map((side) => {
          const source = side === 'left' ? left : right;
          const name = side === 'left' ? key.left_column : key.right_column;
          return <PanelSelect key={side} testId={`field-join_key_${side}-${index}`}
            ariaLabel={`Key pair ${index + 1} ${side} column`} value={name} disabled={blocked}
            options={[...(!source?.columns.some((column) => column.name === name)
              ? [{ value: name, label: name || 'Choose a column' }] : []),
              ...(source?.columns ?? []).map((column) => ({ value: column.name, label: column.name }))]}
            onValueChange={(name) => setParams({ ...params, join_keys: keys.map((pair, i) => i === index
              ? { ...pair, [side === 'left' ? 'left_column' : 'right_column']: name } : pair) })} />;
        })}
        <button type="button" className="icon-btn" aria-label={`Remove key pair ${index + 1}`}
          data-testid={`join-key-pair-remove-${index}`} onClick={() => setParams({ ...params,
            join_keys: keys.filter((_, i) => i !== index) })}><X size={13} /></button>
      </div>)}
    </div>
    <button type="button" className="btn btn-ghost" data-testid="join-key-pair-add" disabled={loading || keys.length >= 8}
      onClick={() => setParams({ ...params, join_keys: [...keys,
        { left_column: left.columns[0]?.name ?? '', right_column: right?.columns[0]?.name ?? '' }] })}>
      <Plus size={13} /> Add key</button>
    <div className="form-row-pair">
      {(['left', 'right'] as const).map((side) => <div key={side}>
        <span className="form-label">{side === 'left' ? 'Left' : 'Right'} columns to include</span>
        <MultiColumnPicker testId={`field-join_${side}_columns`} ariaLabel={`${side} columns to include`}
          value={[...new Set((columns ?? []).filter((column) => column.side === side).map((column) => column.column))]}
          options={((side === 'left' ? left : right)?.columns ?? []).map(({ name, type, ai }) =>
            ({ name, type, ai: Boolean(ai) }))} disabled={blocked}
          onValueChange={(names) => changeProjection(side, names)} />
      </div>)}
    </div>
    <p className="form-hint">Leave both sides empty for all non-key columns. After any pick, only picked columns are included.</p>
    {columns != null && <div data-testid="join-projection-order">
      <span className="form-label">Projection order</span>
      {columns.map((column, index) => <div className="tabular-join-key-pair-row" key={index}>
        <span>{column.side}:</span>
        <PanelSelect ariaLabel={`Projection ${index + 1} column`} testId={`join-projection-column-${index}`}
          value={column.column} disabled={blocked}
          options={[...(!column.column ? [{ value: '', label: 'Choose replacement' }] : []),
            ...((column.side === 'left' ? left : right)?.columns ?? []).map((source) => ({ value: source.name, label: source.name }))]}
          onValueChange={(name) => setParams({ ...params, columns: columns.map((item, i) => index === i
            ? { ...item, column: name } : item) })} />
        <button type="button" className="icon-btn" aria-label={`Move projection ${index + 1} up`}
          disabled={index === 0} onClick={() => move(index, -1)}><ArrowUp size={13} /></button>
        <button type="button" className="icon-btn" aria-label={`Move projection ${index + 1} down`}
          disabled={index === columns.length - 1} onClick={() => move(index, 1)}><ArrowDown size={13} /></button>
        <button type="button" className="icon-btn" aria-label={`Duplicate projection ${index + 1}`}
          onClick={() => setParams({ ...params, columns: [...columns.slice(0, index + 1), column,
            ...columns.slice(index + 1)] })}><Copy size={13} /></button>
        <button type="button" className="icon-btn" aria-label={`Remove projection ${index + 1}`}
          onClick={() => { const next = columns.filter((_, i) => i !== index);
            setParams({ ...params, columns: next.length ? next : null }); }}><X size={13} /></button>
      </div>)}
    </div>}
    <Field name="how" label="How" />
    <ToggleRow title="Include a 'which side' column" description="Records whether each row matched both sheets or only one."
      checked={params.indicator ?? false} testId="field-join_indicator"
      onCheckedChange={(indicator) => setParams({ ...params, indicator })} />
    <details className="action-advanced"><summary>Advanced options</summary>
      <Field name="max_output_rows" label="Maximum output rows" /></details>
    </fieldset>
  </div>;
}
