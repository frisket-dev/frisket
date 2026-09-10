import { useEffect, useRef, useState } from 'react';
import { parseRangeDraftText } from '../../temporal/draft';
import { formatTimecode } from '../../temporal/model';
import { RangeDraftPreview } from '../temporal/TemporalDraftEditor';
import { PanelSelect } from '../PanelSelect';
import { SegmentedToggle } from '../PanelPrimitives';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

/** One-range editor; common host owns scope, names, validation and execution. */
export function TemporalExtractParamsBody({ sheet, params, setParams, request, Field }:
  GeneratedActionParamsBodyProps<'temporal.extract_range'>) {
  const selection = params.selection;
  const fromColumn = selection?.kind === 'column';
  const richSelection = selection?.kind === 'typed_value';
  const [start, setStart] = useState(() => selection?.kind === 'draft_range'
    ? formatTimecode(selection.start_ms) : '');
  const [end, setEnd] = useState(() => selection?.kind === 'draft_range'
    ? formatTimecode(selection.end_ms) : '');
  const rangeColumns = (sheet?.columns ?? []).filter((column) => column.type === 'timeline_range');
  const selectedColumn = fromColumn
    ? rangeColumns.find((column) => column.name === selection.column) : undefined;
  const rowIds = request.scope.kind === 'sheet_rows' ? request.scope.row_ids : undefined;
  const affectedRows = rowIds?.length ?? 0;
  const repeat = selection?.kind !== 'column' && selection?.repeat_for_rows === true;
  const identity = JSON.stringify({ source: params.source, scope: request.scope,
    selection: selection?.kind === 'column' ? selection : { ...selection, repeat_for_rows: false } });
  const previousIdentity = useRef<string | null>(null);
  useEffect(() => {
    const changed = previousIdentity.current !== identity;
    previousIdentity.current = identity;
    if (changed && repeat && selection) {
      setParams({ ...params, selection: { ...selection, repeat_for_rows: false } });
    }
  }, [identity, params, repeat, selection, setParams]);
  const parsed = parseRangeDraftText(`${start}\t${end}`);
  const updateRange = (nextStart: string, nextEnd: string) => {
    setStart(nextStart);
    setEnd(nextEnd);
    const value = parseRangeDraftText(`${nextStart}\t${nextEnd}`);
    // Invalid input stays invalid in the submitted Params, never the last valid interval.
    const item = value.ok && value.items.length === 1 ? value.items[0] : undefined;
    setParams({ ...params, selection: { kind: 'draft_range',
      start_ms: item?.start_ms ?? 0, end_ms: item?.end_ms ?? 0, repeat_for_rows: false } });
  };
  return <div className="temporal-action-body" data-testid="temporal-extract-params">
    <Field name="source" label="Audio or video" testId="extract-range-source" />
    {!(sheet?.columns ?? []).some((column) => column.type === 'audio' || column.type === 'video') && (
      <p role="status" data-testid="temporal-source-empty">Add a video or audio column before extracting a range.</p>
    )}
    <div className="field-group"><span className="field-labels">Selection</span>
      <SegmentedToggle ariaLabel="Selection source" value={fromColumn ? 'column' : richSelection ? '' : 'manual'}
        buttonTestId={(value) => `extract-range-selection-${value}`} options={[
          { value: 'manual', label: 'Enter range' },
          { value: 'column', label: 'Use a column', disabledReason: rangeColumns.length === 0
            ? 'No time-range column is available.' : undefined },
        ]} onValueChange={(value) => {
          if (value === 'column' && rangeColumns[0]) {
            setParams({ ...params, selection: { kind: 'column', column: rangeColumns[0].name } });
          } else updateRange(start, end);
        }} />
    </div>
    {fromColumn ? <label className="field-group"><span className="field-labels">Time-range column</span>
      <PanelSelect className="form-input" data-testid="extract-range-column-select"
        value={selectedColumn?.name ?? ''} onChange={(event) => setParams({ ...params,
          selection: { kind: 'column', column: event.target.value } })}>
        {!selectedColumn && <option value="" disabled>Saved selection unavailable</option>}
        {rangeColumns.map((column) => <option key={column.id} value={column.name}>{column.name}</option>)}
      </PanelSelect>
      <span className="form-hint">Each row uses its own time range. Frisket checks that it belongs to the selected source.
        {rowIds === undefined && ` With no selected rows, this will run on all ${sheet?.rowCount ?? 0} rows.`}</span>
    </label> : richSelection ? <>
      <p className="form-hint">This saved selection retains its exact timeline value. Choose Enter range to replace it.</p>
      <Field name="selection" label="Saved temporal selection" />
    </> : <>
      <div className="dense-grid">
        <label className="field-group"><span className="field-labels">Start</span>
          <input className="form-input" data-testid="extract-range-start" value={start}
            placeholder="00:10" onChange={(event) => updateRange(event.target.value, end)} />
        </label>
        <label className="field-group"><span className="field-labels">End</span>
          <input className="form-input" data-testid="extract-range-end" value={end}
            placeholder="00:45" onChange={(event) => updateRange(start, event.target.value)} />
        </label>
      </div>
      <p className="form-hint">Bare numbers are seconds; timecodes and explicit units such as 90000ms also work.</p>
      {parsed.ok ? <RangeDraftPreview items={parsed.items} purpose="media" />
        : (start.trim() || end.trim()) && <p role="alert" className="temporal-validation-error"
          data-testid="extract-range-error">{parsed.error}</p>}
    </>}
    {!fromColumn && affectedRows === 0 && <p role="alert" data-testid="extract-range-row-required">
      Select a row, or open a row in Detail, before extracting a range.
    </p>}
    {!fromColumn && affectedRows > 1 && selection && <label className="temporal-batch-confirm">
      <input type="checkbox" checked={repeat} data-testid="extract-range-batch-confirm"
        onChange={(event) => setParams({ ...params,
          selection: { ...selection, repeat_for_rows: event.target.checked } })} />
      <span>Apply this same range to {affectedRows} rows. Frisket will check that it fits each source.</span>
    </label>}
  </div>;
}
