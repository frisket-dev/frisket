import { useEffect, useRef, useState } from 'react';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import {
  formatPointDraftText,
  formatRangeDraftText,
  parsePointDraftText,
  parseRangeDraftText,
} from '../../temporal/draft';
import { PanelSelect } from '../PanelSelect';
import { SegmentedToggle } from '../PanelPrimitives';
import { PointDraftPreview, RangeDraftPreview } from '../temporal/TemporalDraftEditor';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

type Params = GeneratedActionParams['derive.transcript_segments'];
type Selection = Params['selection'];
type Mode = 'points' | 'ranges';

const POINT_TYPES = new Set(['timeline_point', 'timeline_points']);
const RANGE_TYPES = new Set(['timeline_range', 'timeline_ranges']);

function withoutRepetition(selection: Selection | undefined) {
  if (!selection || selection.kind === 'column') return selection;
  return { ...selection, repeat_for_rows: false };
}

/** Edits semantic transcript selection only; naming, scope, Run and jobs remain
 * with the generated host. Timeline compatibility is checked by that host. */
export function TranscriptSegmentsParamsBody({
  ...props
}: GeneratedActionParamsBodyProps<'derive.transcript_segments'>) {
  return <TemporalSegmentsEditor {...props} purpose="transcript" />;
}

export function MediaSegmentsParamsBody(props: GeneratedActionParamsBodyProps<'derive.temporal_segments'>) {
  return <TemporalSegmentsEditor {...props} purpose="media" />;
}

function TemporalSegmentsEditor({
  sheet, params, setParams, request, Field,
  purpose,
}: GeneratedActionParamsBodyProps<'derive.transcript_segments' | 'derive.temporal_segments'> & {
  purpose: 'media' | 'transcript';
}) {
  const selection = params.selection;
  const initialColumn = selection?.kind === 'column'
    ? (sheet?.columns ?? []).find((column) => column.name === selection.column)
    : undefined;
  const [mode, setMode] = useState<Mode>(
    selection?.kind === 'draft_ranges' || selection?.kind === 'draft_range'
      || (initialColumn && RANGE_TYPES.has(initialColumn.type)) ? 'ranges' : 'points',
  );
  const [pointText, setPointText] = useState(() => selection?.kind === 'draft_points'
    ? formatPointDraftText(selection.items.map((item, index) => ({
      ...item, id: String(index + 1), ...(item.label == null ? { label: undefined } : {}),
    }))) : '');
  const [rangeText, setRangeText] = useState(() => selection?.kind === 'draft_ranges'
    ? formatRangeDraftText(selection.items.map((item, index) => ({
      ...item, id: String(index + 1), ...(item.label == null ? { label: undefined } : {}),
    }))) : '');
  const fromColumn = selection?.kind === 'column';
  const richSelection = selection?.kind === 'typed_value' || selection?.kind === 'draft_range';
  const selectionColumns = (sheet?.columns ?? []).filter((column) => (
    mode === 'points' ? POINT_TYPES : RANGE_TYPES
  ).has(column.type));
  const selectedColumn = fromColumn
    ? selectionColumns.find((column) => column.name === selection.column)
    : undefined;
  const rowIds = request.scope.kind === 'sheet_rows' ? request.scope.row_ids : undefined;
  const affectedRows = rowIds?.length ?? 0;
  const literalBatch = !fromColumn && affectedRows > 1;
  const repeat = selection?.kind !== 'column' && selection?.repeat_for_rows === true;
  const resetIdentity = JSON.stringify({
    source: params.source,
    selection: withoutRepetition(selection),
    scope: request.scope,
  });
  const previousIdentity = useRef<string | null>(null);
  useEffect(() => {
    const changed = previousIdentity.current !== resetIdentity;
    previousIdentity.current = resetIdentity;
    // Reopening saved intent also requires a fresh visible acknowledgement.
    // The checkbox itself has one authority: canonical selection Params.
    if (changed && repeat && selection) {
      setParams({ ...params, selection: { ...selection, repeat_for_rows: false } });
    }
  }, [params, repeat, resetIdentity, selection, setParams]);

  const setSelection = (next: Selection) => setParams({
    ...params, selection: next.kind === 'column' ? next : { ...next, repeat_for_rows: false },
  });
  const parsedPoints = parsePointDraftText(pointText);
  const parsedRanges = parseRangeDraftText(rangeText);
  const pointsError = parsedPoints.ok
    ? parsedPoints.items.some((item) => item.at_ms > 0)
      ? null : 'Add at least one timestamp after 0:00.'
    : pointText.trim() ? parsedPoints.error : null;
  const setPoints = (text: string) => {
    setPointText(text);
    const parsed = parsePointDraftText(text);
    setSelection({ kind: 'draft_points', repeat_for_rows: false,
      items: parsed.ok && parsed.items.some((item) => item.at_ms > 0)
        ? parsed.items.map(({ at_ms, label }) => ({
        at_ms, ...(label ? { label } : {}),
      })) : [],
    });
  };
  const setRanges = (text: string) => {
    setRangeText(text);
    const parsed = parseRangeDraftText(text);
    setSelection({ kind: 'draft_ranges', repeat_for_rows: false,
      items: parsed.ok ? parsed.items.map(({ start_ms, end_ms, label }) => ({
        start_ms, end_ms, ...(label ? { label } : {}),
      })) : [],
    });
  };
  const enterManual = (nextMode: Mode) => {
    setMode(nextMode);
    if (nextMode === 'points') setPoints(pointText);
    else setRanges(rangeText);
  };

  return <div className="temporal-action-body" data-testid={`${purpose}-segments-params`}>
    <Field name="source" label={purpose === 'media' ? 'Audio or video' : 'Timestamped transcript'} testId="split-source" />
    {!(sheet?.columns ?? []).some((column) => purpose === 'media'
      ? column.type === 'audio' || column.type === 'video' : column.type === 'timestamped_transcript') && (
      <p className="form-hint" role="status" data-testid="temporal-source-empty">
        {purpose === 'media' ? 'Add a video or audio column before splitting.'
          : 'Transcribe audio or video with timestamps before splitting a transcript.'}
      </p>
    )}
    <div className="field-group">
      <span className="field-labels">Split using</span>
      <SegmentedToggle options={[
        { value: 'points', label: 'Timestamps' },
        { value: 'ranges', label: 'Time ranges' },
      ]} value={mode} onValueChange={(value) => enterManual(value as Mode)}
      ariaLabel="Split using" buttonTestId={(value) => `split-mode-${value}`} />
    </div>
    <div className="field-group">
      <span className="field-labels">Selection</span>
      <SegmentedToggle options={[
        { value: 'manual', label: mode === 'points' ? 'Enter timestamps' : 'Enter ranges' },
        { value: 'column', label: 'Use a column', disabledReason: selectionColumns.length === 0
          ? `No ${mode === 'points' ? 'timestamp' : 'time-range'} column is available.` : undefined },
      ]} value={fromColumn ? 'column' : richSelection ? '' : 'manual'}
      onValueChange={(value) => {
        if (value === 'column' && selectionColumns[0]) {
          setSelection({ kind: 'column', column: selectionColumns[0].name });
        } else enterManual(mode);
      }} ariaLabel="Selection source" buttonTestId={(value) => `split-selection-${value}`} />
    </div>
    {fromColumn ? <label className="field-group">
      <span className="field-labels">{mode === 'points' ? 'Timestamp' : 'Time-range'} column</span>
      <PanelSelect className="form-input" value={selectedColumn?.name ?? ''}
        data-testid="split-selection-column-select"
        onChange={(event) => setSelection({ kind: 'column', column: event.target.value })}>
        {!selectedColumn && <option value="" disabled>Saved selection unavailable</option>}
        {selectionColumns.map((column) => <option key={column.id} value={column.name}>{column.name}</option>)}
      </PanelSelect>
      <span className="form-hint">
        Each row uses its own timestamps or ranges. Frisket checks that they belong to that
        row&apos;s selected {purpose === 'media' ? 'source' : 'transcript'} when the action runs.
        Overlapping ranges create overlapping {purpose === 'media' ? 'clips' : 'transcript rows'}.
        {rowIds === undefined && ` With no selected rows, this will run on all ${sheet?.rowCount ?? 0} rows.`}
      </span>
    </label> : richSelection ? <>
      <p className="form-hint">This saved selection retains its exact timeline value or single range.
        Choose timestamps or time ranges above to replace it with a pasted draft.</p>
      <Field name="selection" label="Saved temporal selection" />
    </> : mode === 'points' ? <>
      <label className="field-group"><span className="field-labels">Timestamps</span>
        <textarea className="form-input temporal-paste-input" rows={7} spellCheck={false}
          value={pointText} data-testid="split-points-textarea"
          placeholder={'00:30\n01:15,Opening\n02:45\tInterview begins'}
          onChange={(event) => setPoints(event.target.value)} />
        <span className="form-hint">One timestamp per line. Add an optional label in a second CSV or spreadsheet column.
          Bare numbers are seconds; timecodes and explicit units such as 01:30 or 90000ms also work.</span>
      </label>
      {pointsError ? <p role="alert" className="temporal-validation-error" data-testid="split-points-error">{pointsError}</p>
        : parsedPoints.ok && <PointDraftPreview items={parsedPoints.items} purpose={purpose} />}
    </> : <>
      <label className="field-group"><span className="field-labels">Time ranges</span>
        <textarea className="form-input temporal-paste-input" rows={7} spellCheck={false}
          value={rangeText} data-testid="split-ranges-textarea"
          placeholder={'00:10,00:45,Opening\n01:20,02:00,Interview'}
          onChange={(event) => setRanges(event.target.value)} />
        <span className="form-hint">One range per line: start, end, and an optional label. Paste CSV or spreadsheet columns.</span>
      </label>
      {parsedRanges.ok ? <RangeDraftPreview items={parsedRanges.items} purpose={purpose} />
        : rangeText.trim() && <p role="alert" className="temporal-validation-error" data-testid="split-ranges-error">{parsedRanges.error}</p>}
    </>}
    {!fromColumn && affectedRows === 0 && <p role="alert" className="temporal-validation-error" data-testid="split-row-required">
      Select a row, or open a row in Detail, before splitting it.
    </p>}
    {literalBatch && selection && <label className="temporal-batch-confirm">
      <input type="checkbox" checked={repeat} data-testid="split-batch-confirm"
        onChange={(event) => setParams({ ...params,
          selection: { ...selection, repeat_for_rows: event.target.checked },
        })} />
      <span>Apply these same timestamps or ranges to {affectedRows} rows. Frisket will check that they fit each source.</span>
    </label>}
  </div>;
}
