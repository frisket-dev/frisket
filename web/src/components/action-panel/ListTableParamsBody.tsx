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
      <label className="form-label" htmlFor="list-table-column">List column to materialize</label>
      <select id="list-table-column" className="form-input" data-testid="derive-source-column-select"
        disabled={!sheet} value={liveSource?.column_id ?? ''} onChange={(event) => sheet && setParams({ ...params,
          source: { ...liveSource, kind: 'column', sheet_id: Number(sheet.id),
            column_id: Number(event.target.value) },
        })}>
        <option value="" disabled>Choose a JSON list column</option>
        {listColumns.map((column) => <option key={column.id} value={column.id}>{column.name}</option>)}
      </select>
      <p className="form-hint">Each list item becomes a row. Columns are discovered from the stored values.</p>
      {liveSource && <>
        <label className="form-label" htmlFor="list-table-properties">Include properties</label>
        <input id="list-table-properties" className="form-input"
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
    <details className="action-advanced" data-testid="derive-table-saved-configuration">
      <summary>Source and column mapping</summary>
      <pre data-testid="derive-table-saved-configuration-json">{JSON.stringify(source, null, 2)}</pre>
      {namedSource && <><label className="form-label" htmlFor="list-table-projection">Column projection and item schema</label>
      <textarea id="list-table-projection" className="form-input" rows={7}
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
      {configurationError && <p role="alert" className="form-error">Enter an object containing columns and item_schema.</p>}</>}
    </details>
  </div>;
}
