import '../PanelPrimitives.css';
import '../ActionPanel.css';
import { TextInput, TextArea } from '../FormControls';
import { MultiColumnPicker } from '../MultiColumnPicker';
import { useEffect, useRef, useState } from 'react';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export function ListTableParamsBody({ sheet, params, setParams }:
  GeneratedActionParamsBodyProps<'derive.table_from_list'>) {
  const source = params.source;
  const listColumns = (sheet?.columns ?? []).filter((column) => column.type === 'json');
  const [configuration, setConfiguration] = useState(() => JSON.stringify({
    ...(params.columns == null ? {} : { columns: params.columns }),
    ...(params.item_schema == null ? {} : { item_schema: params.item_schema }),
  }, null, 2));
  const [configurationError, setConfigurationError] = useState(false);
  useEffect(() => {
    if (source && typeof source === 'object') return;
    const first = listColumns[0];
    if (first && sheet) setParams({ ...params, source: {
      kind: 'column', sheet_id: Number(sheet.id), column_id: Number(first.id),
    } });
  }, [source, params, setParams, listColumns, sheet]);
  const liveSource = source && typeof source === 'object' && source.kind === 'column'
    ? source : null;
  const namedSource = source && typeof source === 'object' && source.kind === 'named_result'
    ? source : null;
  const parentColumns = params.parent_columns ?? [];
  const parentOptions = (sheet?.columns ?? []).filter(
    (column) => Number(column.id) !== Number(source?.column_id),
  );
  const parentSelections = parentColumns.map((selected) => {
    const column = parentOptions.find((candidate) => Number(candidate.id) === selected.column_id);
    return { selected, column, label: column?.name ?? `Missing column #${selected.column_id}` };
  });
  const selectedParentNames = parentSelections.map(({ label }) => label);
  const parentPickerOptions = [
    ...parentOptions.map(({ name, type, ai }) => ({ name, type, ai: Boolean(ai) })),
    ...parentSelections.filter(({ column }) => !column)
      .map(({ label }) => ({ name: label, type: 'missing', ai: false })),
  ];
  const [includeText, setIncludeText] = useState(() => liveSource?.include_columns?.join(', ') ?? '');
  const editedSource = useRef(liveSource);
  useEffect(() => {
    if (liveSource === editedSource.current) return;
    editedSource.current = liveSource;
    setIncludeText(liveSource?.include_columns?.join(', ') ?? '');
  }, [liveSource]);
  return <div className="action-source-block" data-testid="list-table-params">
    {namedSource ? <>
      <div className="form-label">Saved list source</div>
      <p className="form-hint" data-testid="derive-table-source-summary">
        Named result {namedSource.route} from run {namedSource.run_id} on sheet {namedSource.sheet_id}
      </p>
    </> : <>
      <label className="form-label" htmlFor="list-table-column">List</label>
      <select id="list-table-column" className="form-input" data-testid="derive-source-column-select"
        disabled={!sheet} value={liveSource?.column_id ?? ''} onChange={(event) => {
          if (!sheet) return;
          const columnId = Number(event.target.value);
          setParams({ ...params,
            source: { ...liveSource, kind: 'column', sheet_id: Number(sheet.id), column_id: columnId },
            parent_columns: parentColumns.filter((column) => column.column_id !== columnId),
          });
        }}>
        <option value="" disabled>Choose a JSON list column</option>
        {listColumns.map((column) => <option key={column.id} value={column.id}>{column.name}</option>)}
      </select>
      <p className="form-hint">Each list item becomes a row. Columns are discovered from the stored values.</p>
      {liveSource && <>
        <label className="form-label" htmlFor="list-table-properties">Include properties</label>
        <TextInput id="list-table-properties"
          placeholder="All properties" value={includeText}
          onChange={(event) => {
            const text = event.target.value;
            const columns = text.split(',').map((name) => name.trim()).filter(Boolean);
            const nextSource = { ...liveSource, include_columns: columns.length ? columns : null };
            editedSource.current = nextSource;
            setIncludeText(text);
            setParams({ ...params, source: nextSource });
          }} />
      </>}
    </>}
    <div className="field-group"><span className="form-label">Include parent columns</span>
      <MultiColumnPicker testId="field-list_parent_columns" ariaLabel="Include parent columns"
        value={selectedParentNames}
        options={parentPickerOptions}
        disabled={!sheet} onValueChange={(names) => {
          const byId = new Map(parentColumns.map((column) => [column.column_id, column]));
          setParams({ ...params, parent_columns: names.flatMap((name) => {
            const saved = parentSelections.find(({ label }) => label === name)?.selected;
            if (saved) return [saved];
            const column = parentOptions.find((candidate) => candidate.name === name);
            if (!column) return [];
            const columnId = Number(column.id);
            return [{ column_id: columnId,
              output_name: byId.get(columnId)?.output_name ?? column.name }];
          }) });
        }} />
      <span className="form-hint">Copied into each expanded row. Refresh updates copies after parent edits.</span>
    </div>
    <details className="action-advanced" data-testid="derive-table-saved-configuration">
      <summary>Source and column mapping</summary>
      <pre data-testid="derive-table-saved-configuration-json">{JSON.stringify(source, null, 2)}</pre>
      {parentSelections.length > 0 && <div className="action-source-block">
        <div className="form-label">Parent column destinations</div>
        {parentSelections.map(({ selected, column, label }) => <div className="field-group"
          key={selected.column_id} data-testid={`list-parent-column-${selected.column_id}`}>
          <span className={column ? 'form-hint' : 'panel-error'}>{label}</span>
          <TextInput aria-label={`Destination for ${label}`} value={selected.output_name}
            onChange={(event) => setParams({ ...params, parent_columns: parentColumns.map(
              (candidate) => candidate.column_id === selected.column_id
                ? { ...candidate, output_name: event.target.value }
                : candidate,
            ) })} />
          {!column && <button type="button" className="btn-ghost"
            aria-label={`Remove missing parent column #${selected.column_id}`} onClick={() => setParams({ ...params,
              parent_columns: parentColumns.filter(
                (candidate) => candidate.column_id !== selected.column_id,
              ),
            })}>Remove</button>}
        </div>)}
      </div>}
      {namedSource && <><label className="form-label" htmlFor="list-table-projection">Column projection and item schema</label>
      <TextArea id="list-table-projection"  rows={7}
        value={configuration} onChange={(event) => {
          setConfiguration(event.target.value);
          try {
            const parsed: unknown = JSON.parse(event.target.value);
            if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)
              || Object.keys(parsed).some((key) => key !== 'columns' && key !== 'item_schema')) throw new Error();
            const projection = parsed as Pick<typeof params, 'columns' | 'item_schema'>;
            setParams({ ...params, columns: projection.columns ?? null,
              item_schema: projection.item_schema ?? null });
            setConfigurationError(false);
          } catch {
            // Send invalid Params through the ordinary resolver, disabling publication.
            setParams({ ...params, columns: [] });
            setConfigurationError(true);
          }
        }} />
      {configurationError && <p role="alert" className="panel-error">Enter an object containing columns and item_schema.</p>}</>}
    </details>
  </div>;
}
