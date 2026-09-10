import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import { ChevronDown, ChevronUp, Columns3, RefreshCcw, Save, Zap } from 'lucide-react';
import {
  type ColumnDef,
  type ColumnPatch,
  type ColumnDisplayFormat,
  type ColumnRun,
  type ColumnRunsInfo,
  type ColumnStats,
  type ColumnType,
  type ColumnTypeInfo,
  type JudgeReviewPassRate,
  type OutputField,
  type ReviewPassRate,
  type RunEstimate,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { ProjectApiPort } from '../api/ports';
import { backfillWithConfirmation } from '../api/backfillWithConfirmation';
import { formatDuration, formatTokens, formatUsdOrNone } from '../format';
import {
  ColumnInspectorRunsSectionFrame,
  ColumnInspectorSettingsSectionFrame,
} from '../workbench/contributions';
import { Drawer } from './Drawer';
import { PanelLoading } from './PanelPrimitives';
import { PanelSelect } from './PanelSelect';

const EMPTY_CONTRIBUTIONS: WorkbenchResolvedLayoutContribution[] = [];

function DetailContributionSlot({
  render,
  contribution,
}: {
  render: (contribution: WorkbenchResolvedLayoutContribution) => ReactNode;
  contribution: WorkbenchResolvedLayoutContribution;
}) {
  return <>{render(contribution)}</>;
}

const LOCAL_RUN_KINDS = new Set([
  'media.extract_faces',
  'media.extract_metadata',
  'media.video_frames',
  'media.transcribe',
  'media.ocr',
  'media.to_markdown',
  'map.regex_extract',
  'map.template',
  'map.python',
  'research.web_search',
  'regex_extract',
  'web_search',
]);

const COLUMN_RUN_HISTORY_PAGE_SIZE = 20;

const CORE_COLUMN_TYPES: ColumnTypeInfo[] = [
  'text', 'number', 'integer', 'boolean', 'category', 'json', 'date',
  'image', 'audio', 'video', 'file', 'link', 'geo_point',
].map((name) => ({
  name,
  core: true,
  presentation: {},
  hasValidator: true,
  hasParser: false,
  description: '',
})).concat([
  { name: 'timeline_point', label: 'Timestamp' },
  { name: 'timeline_points', label: 'Timestamps' },
  { name: 'timeline_range', label: 'Time range' },
  { name: 'timeline_ranges', label: 'Time ranges' },
].map(({ name, label }) => ({
  name,
  core: true,
  presentation: { renderer: 'timeline', label, userSelectable: false },
  hasValidator: true,
  hasParser: true,
  description: '',
})));

const FORMAT_OPTIONS: Array<{
  value: ColumnDisplayFormat;
  label: string;
  types: ColumnType[];
}> = [
  { value: 'markdown', label: 'Markdown', types: ['text'] },
  { value: 'filesize', label: 'File size', types: ['integer', 'number'] },
  { value: 'currency', label: 'Currency', types: ['integer', 'number'] },
  { value: 'percent', label: 'Percent', types: ['integer', 'number'] },
];

export interface ColumnDrawerProps {
  column: ColumnDef;
  sheetId: string;
  onClose(): void;
  onBackfillComplete(runId: string, filled: number): void;
  onColumnUpdated(column: ColumnDef): void;
  /** Opens the existing review queue scoped to one persisted action run. */
  onReviewRun(runId: string): void;
  /** An over-gate run.backfill 402s; the drawer routes
   *  it through the SAME priced cost-confirmation surface a fresh run uses
   *  (the run controller's requestCostConfirmation → shared CostGateModal). */
  requestCostConfirmation(estimate: RunEstimate, message: string): Promise<boolean>;
  /** Resolved columnDetail/columnInspector plugin contributions + the
   *  workspace dispatch that mounts them.
   *  First-party settings/runs sections keep their content-driven positions. */
  detailContributions?: WorkbenchResolvedLayoutContribution[];
  renderDetailContribution?(contribution: WorkbenchResolvedLayoutContribution): ReactNode;
}

/** Pull the human-readable prompt out of a run spec (current backend run specs name it differently). */
function specPrompt(spec: Record<string, unknown>): string {
  for (const key of ['context', 'instruction', 'query', 'prompt']) {
    const v = spec[key];
    if (typeof v === 'string' && v.trim()) return v;
  }
  return '';
}

function specFields(spec: Record<string, unknown>): OutputField[] {
  const fields = spec.fields;
  if (!Array.isArray(fields)) return [];
  return fields.filter(
    (f): f is OutputField => f !== null && typeof f === 'object' && typeof (f as OutputField).name === 'string',
  );
}

type ColumnSettingsDraft = {
  columnId: string;
  type: ColumnType;
  format: ColumnDisplayFormat | '';
};

type ColumnUpdateState = {
  columnId: string;
  busy: boolean;
  message: string | null;
  error: string | null;
};

type LoadedColumnRuns =
  | { id: string; info: ColumnRunsInfo; error?: undefined }
  | { id: string; info?: undefined; error: string };

function columnSettingsDraft(column: ColumnDef): ColumnSettingsDraft {
  return {
    columnId: column.id,
    type: column.type,
    format: (column.format as ColumnDisplayFormat | null) ?? '',
  };
}

function columnUpdateState(columnId: string): ColumnUpdateState {
  return { columnId, busy: false, message: null, error: null };
}

function fetchColumnRuns(
  api: Pick<ProjectApiPort, 'getColumnRuns'>,
  columnId: string,
  offset = 0,
  limit = COLUMN_RUN_HISTORY_PAGE_SIZE,
): Promise<LoadedColumnRuns> {
  return api
    .getColumnRuns(columnId, offset, limit)
    .then((info) => ({ id: columnId, info }))
    .catch((e: Error) => ({ id: columnId, error: e.message }));
}

function columnUpdateErrorMessage(error: unknown, typeDraft: ColumnType): string {
  const raw = error instanceof Error ? error.message : String(error);
  const lower = raw.toLowerCase();
  if (
    lower.includes('column_value_validation_failed') ||
    lower.includes('existing values fail validation') ||
    lower.includes('fail validation for target type')
  ) {
    return `Existing values in this column cannot be saved as ${typeDraft}. Edit the values or choose another type.`;
  }
  if (lower.includes('invalid_column_format') || lower.includes('display format')) {
    return 'That display format is not valid for this column type. Choose an available format and try again.';
  }
  if (lower.includes('invalid_column_type') || lower.includes('type is not registered')) {
    return `The ${typeDraft} column type is not available in this project. Choose another type.`;
  }
  return raw;
}

/** "click an AI column → drawer: prompt, model, cost, version history". */
export function ColumnDrawer({
  column,
  sheetId,
  onClose,
  onBackfillComplete,
  onColumnUpdated,
  onReviewRun,
  requestCostConfirmation,
  detailContributions = EMPTY_CONTRIBUTIONS,
  renderDetailContribution,
}: ColumnDrawerProps) {
  const ai = column.ai;

  return (
    <Drawer
      testId="column-drawer"
      title={
        <>
          {ai ? <Zap size={13} className="ai-bolt" /> : <Columns3 size={13} />}
          {column.name}
          <span className="muted"> · {ai ? 'AI column' : 'column'}</span>
        </>
      }
      onClose={onClose}
    >
      <section className="col-meta">
        <div className="col-meta-row">
          <span className="prov-key">type</span>
          <span>{column.type}</span>
        </div>
        <div className="col-meta-row">
          <span className="prov-key">format</span>
          <span>{column.format ?? 'default'}</span>
        </div>
      </section>

      <ColumnInspectorSettingsSectionFrame>
        <ColumnSettingsSection column={column} onColumnUpdated={onColumnUpdated} />
      </ColumnInspectorSettingsSectionFrame>

      <ColumnStatsSection column={column} sheetId={sheetId} />

      {ai && (
        <ColumnInspectorRunsSectionFrame>
          <AiColumnDetails
            column={column}
            sheetId={sheetId}
            onBackfillComplete={onBackfillComplete}
            requestCostConfirmation={requestCostConfirmation}
            onReviewRun={onReviewRun}
          />
        </ColumnInspectorRunsSectionFrame>
      )}
      {renderDetailContribution &&
        detailContributions
          .flatMap((contribution) => {
            // Plugin contributions only: first-party settings/runs sections
            // keep their content-driven interleaved positions.
            if (
              contribution.runtimeSource !== 'runtimeIndex' ||
              (contribution.status !== 'enabled' && contribution.status !== 'disabled')
            ) return [];
            return [(
              <div
                key={`${contribution.contributionId}:${contribution.placementId}`}
                className="column-detail-contribution"
                data-testid={`column-detail-contribution-${contribution.contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`}
                data-contribution-id={contribution.contributionId}
                data-status={contribution.status}
                data-runtime-source={contribution.runtimeSource}
              >
                <DetailContributionSlot render={renderDetailContribution} contribution={contribution} />
              </div>
            )];
          })}
    </Drawer>
  );
}

type ColumnStatsState =
  | { columnId: string; phase: 'loading'; force: boolean }
  | { columnId: string; phase: 'ready'; stats: ColumnStats }
  | { columnId: string; phase: 'error'; message: string };

const STAT_NUMBER_FORMATTER = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
const FILE_SIZE_FORMATTERS = [
  new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }),
  new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }),
  new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }),
];

function formatStatNumber(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return '—';
  return STAT_NUMBER_FORMATTER.format(value);
}

function formatFileSize(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return '—';
  const units = ['bytes', 'KB', 'MB', 'GB'];
  let size = Math.max(0, value);
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 ? 0 : size >= 10 ? 1 : 2;
  return `${FILE_SIZE_FORMATTERS[digits].format(size)} ${units[unitIndex]}`;
}

function formatStatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString();
}

function truncateStatValue(value: string): string {
  return value.length > 90 ? `${value.slice(0, 87)}...` : value;
}

function ColumnStatsSection({ column, sheetId }: { column: ColumnDef; sheetId: string }) {
  const { projectApi } = useWorkspaceStores();
  const [state, setState] = useState<ColumnStatsState>({
    columnId: column.id,
    phase: 'loading',
    force: false,
  });
  const statsRequestRef = useRef(0);

  const loadStats = (force = false) => {
    const requestId = statsRequestRef.current + 1;
    statsRequestRef.current = requestId;
    setState({ columnId: column.id, phase: 'loading', force });
    void projectApi
      .getColumnStats(sheetId, column.id, { force })
      .then((stats) => {
        if (statsRequestRef.current === requestId) {
          setState({ columnId: column.id, phase: 'ready', stats });
        }
      })
      .catch((e: Error) => {
        if (statsRequestRef.current === requestId) {
          setState({ columnId: column.id, phase: 'error', message: e.message });
        }
      });
  };

  useEffect(() => {
    const requestId = statsRequestRef.current + 1;
    statsRequestRef.current = requestId;
    let alive = true;
    void projectApi
      .getColumnStats(sheetId, column.id)
      .then((stats) => {
        if (alive && statsRequestRef.current === requestId) {
          setState({ columnId: column.id, phase: 'ready', stats });
        }
      })
      .catch((e: Error) => {
        if (alive && statsRequestRef.current === requestId) {
          setState({ columnId: column.id, phase: 'error', message: e.message });
        }
      });
    return () => {
      alive = false;
    };
  }, [column.id, sheetId]);

  const visibleState: ColumnStatsState =
    state.columnId === column.id ? state : { columnId: column.id, phase: 'loading', force: false };

  return (
    <section className="column-stats" data-testid="column-stats-section">
      <h3 className="drawer-section-title">Column statistics</h3>
      {visibleState.phase === 'loading' ? (
        <PanelLoading
          testId="column-stats-loading"
          label={visibleState.force ? 'analyzing column...' : 'loading statistics...'}
        />
      ) : visibleState.phase === 'error' ? (
        <div className="form-error" data-testid="column-stats-error">{visibleState.message}</div>
      ) : visibleState.stats.requiresManualAnalyze ? (
        <div className="column-stats-deferred" data-testid="column-stats-deferred">
          <p>
            This column has {visibleState.stats.rowCount.toLocaleString()} rows. Analyze it when you
            are ready to scan the full column.
          </p>
          <button
            type="button"
            className="btn column-save"
            data-testid="analyze-column-button"
            onClick={() => loadStats(true)}
          >
            Analyze column
          </button>
        </div>
      ) : (
        <ColumnStatsResult stats={visibleState.stats} />
      )}
    </section>
  );
}

function ColumnStatsResult({ stats }: { stats: ColumnStats }) {
  const topValues = stats.topValues ?? [];
  const jsonTypes = stats.jsonTypes ?? [];
  const valueFormatter = stats.column.format === 'filesize' ? formatFileSize : formatStatNumber;
  const formatTopValue = (value: string) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return truncateStatValue(value);
    if (stats.column.format === 'filesize') return formatFileSize(parsed);
    if (stats.numeric) return formatStatNumber(parsed);
    return truncateStatValue(value);
  };
  return (
    <div data-testid="column-stats-result">
      <dl className="column-stats-grid">
        <StatRow label="Rows" value={stats.rowCount.toLocaleString()} testId="column-stats-rows" />
        <StatRow label="Present" value={(stats.present ?? 0).toLocaleString()} testId="column-stats-present" />
        <StatRow label="Missing" value={(stats.missing ?? 0).toLocaleString()} testId="column-stats-missing" />
        {stats.distinct !== undefined && (
          <StatRow label="Distinct" value={stats.distinct.toLocaleString()} testId="column-stats-distinct" />
        )}
      </dl>

      {stats.numeric && (
        <section className="column-stats-subsection" data-testid="column-stats-numeric">
          <h4>{stats.column.format === 'filesize' ? 'File size' : 'Numeric'}</h4>
          <dl className="column-stats-grid">
            <StatRow label="Mean" value={valueFormatter(stats.numeric.mean)} />
            <StatRow label="Median" value={valueFormatter(stats.numeric.median)} />
            <StatRow label="Min" value={valueFormatter(stats.numeric.min)} />
            <StatRow label="Max" value={valueFormatter(stats.numeric.max)} />
          </dl>
          <Histogram bins={stats.numeric.histogram} formatValue={valueFormatter} />
        </section>
      )}

      {stats.date && (
        <section className="column-stats-subsection" data-testid="column-stats-date">
          <h4>Date</h4>
          <dl className="column-stats-grid">
            <StatRow label="Earliest" value={formatStatDate(stats.date.min)} />
            <StatRow label="Median" value={formatStatDate(stats.date.median)} />
            <StatRow label="Latest" value={formatStatDate(stats.date.max)} />
          </dl>
        </section>
      )}

      {stats.file && (
        <section className="column-stats-subsection" data-testid="column-stats-file">
          <h4>Files</h4>
          <dl className="column-stats-grid">
            <StatRow label="Sized files" value={stats.file.count.toLocaleString()} />
            <StatRow label="Min size" value={formatFileSize(stats.file.minSize)} />
            <StatRow label="Max size" value={formatFileSize(stats.file.maxSize)} />
          </dl>
        </section>
      )}

      {stats.text && (
        <section className="column-stats-subsection" data-testid="column-stats-text">
          <h4>Text</h4>
          <dl className="column-stats-grid">
            <StatRow label="Shortest" value={`${stats.text.shortestLength.toLocaleString()} chars`} />
            <StatRow label="Longest" value={`${stats.text.longestLength.toLocaleString()} chars`} />
            <StatRow label="Mean length" value={formatStatNumber(stats.text.meanLength)} />
            <StatRow label="Median length" value={formatStatNumber(stats.text.medianLength)} />
          </dl>
          <div className="column-stats-extreme" data-testid="column-stats-shortest">
            <span>Shortest</span>
            <p>{truncateStatValue(stats.text.shortest)}</p>
          </div>
          <div className="column-stats-extreme" data-testid="column-stats-longest">
            <span>Longest</span>
            <p>{truncateStatValue(stats.text.longest)}</p>
          </div>
        </section>
      )}

      {topValues.length > 0 && (
        <section className="column-stats-subsection">
          <h4>Top values</h4>
          <ol className="column-top-values" data-testid="column-stats-top-values">
            {topValues.map((item) => (
              <li key={item.value}>
                <span title={item.value}>{formatTopValue(item.value)}</span>
                <strong>{item.count.toLocaleString()}</strong>
              </li>
            ))}
          </ol>
        </section>
      )}

      {jsonTypes.length > 0 && (
        <section className="column-stats-subsection" data-testid="column-stats-json-types">
          <h4>Value types</h4>
          <ol className="column-top-values">
            {jsonTypes.map((item) => (
              <li key={item.type}>
                <span>{item.type}</span>
                <strong>{item.count.toLocaleString()}</strong>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function StatRow({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd data-testid={testId}>{value}</dd>
    </div>
  );
}

function Histogram({
  bins,
  formatValue,
}: {
  bins: Array<{ min: number; max: number; count: number }>;
  formatValue: (value: number | undefined) => string;
}) {
  if (bins.length === 0) return null;
  const max = Math.max(...bins.map((bin) => bin.count), 1);
  return (
    <ol className="column-histogram" data-testid="column-stats-histogram">
      {bins.map((bin) => (
        <li
          key={`${bin.min}:${bin.max}`}
          className="column-histogram-bin"
          title={`${formatValue(bin.min)} - ${formatValue(bin.max)}: ${bin.count}`}
        >
          <span>{formatValue(bin.min)} - {formatValue(bin.max)}</span>
          <i><b style={{ width: `${Math.max(3, (bin.count / max) * 100)}%` }} /></i>
          <strong>{bin.count.toLocaleString()}</strong>
        </li>
      ))}
    </ol>
  );
}

function ColumnSettingsSection({
  column,
  onColumnUpdated,
}: {
  column: ColumnDef;
  onColumnUpdated(column: ColumnDef): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const [columnTypes, setColumnTypes] = useState<ColumnTypeInfo[]>(CORE_COLUMN_TYPES);
  const [settingsDraft, setSettingsDraft] = useState<ColumnSettingsDraft | null>(null);
  const [updateState, setUpdateState] = useState<ColumnUpdateState>(() => columnUpdateState(column.id));

  useEffect(() => {
    let alive = true;
    projectApi
      .listColumnTypes()
      .then((types) => { if (alive && types.length) setColumnTypes(types); })
      .catch(() => undefined);
    return () => { alive = false; };
  }, []);

  const currentDraft = settingsDraft?.columnId === column.id
    ? settingsDraft
    : columnSettingsDraft(column);
  const visibleUpdateState = updateState.columnId === column.id
    ? updateState
    : columnUpdateState(column.id);
  const typeDraft = currentDraft.type;
  const formatDraft = currentDraft.format;
  const selectableColumnTypes = columnTypes.filter((type) => (
    type.name === column.type || type.presentation.userSelectable !== false
  ));
  const selectedTypeDescription = columnTypes.find((type) => type.name === typeDraft)?.description;
  const allowedFormats = FORMAT_OPTIONS.filter((option) => option.types.includes(typeDraft));
  const formatValue = allowedFormats.some((option) => option.value === formatDraft) ? formatDraft : '';
  const nextFormat = formatValue === '' ? null : formatValue;
  const hasChanges = typeDraft !== column.type || nextFormat !== (column.format ?? null);

  const setTypeDraft = (next: ColumnType) => {
    setSettingsDraft((current) => {
      const base = current?.columnId === column.id ? current : columnSettingsDraft(column);
      return {
        ...base,
        type: next,
        format: FORMAT_OPTIONS.some((option) => option.value === base.format && option.types.includes(next))
          ? base.format
          : '',
      };
    });
  };

  const setFormatDraft = (next: ColumnDisplayFormat | '') => {
    setSettingsDraft((current) => {
      const base = current?.columnId === column.id ? current : columnSettingsDraft(column);
      return { ...base, format: next };
    });
  };

  const saveColumn = async () => {
    if (!hasChanges || visibleUpdateState.busy) return;
    const savingColumnId = column.id;
    setUpdateState({ columnId: savingColumnId, busy: true, message: null, error: null });
    try {
      const patch: ColumnPatch = {};
      if (typeDraft !== column.type) patch.type = typeDraft;
      if (nextFormat !== (column.format ?? null)) patch.format = nextFormat;
      const updated = await projectApi.updateColumn(column.id, patch);
      onColumnUpdated(updated);
      setSettingsDraft(columnSettingsDraft(updated));
      setUpdateState({
        columnId: savingColumnId,
        busy: false,
        message: 'Column updated.',
        error: null,
      });
    } catch (e: unknown) {
      setUpdateState({
        columnId: savingColumnId,
        busy: false,
        message: null,
        error: columnUpdateErrorMessage(e, typeDraft),
      });
    }
  };

  return (
    <section>
      <h3 className="drawer-section-title">Column settings</h3>
      <div className="column-settings">
        <label className="field-group">
          <span className="field-labels">Type</span>
          <PanelSelect
            className="form-input row-height-select"
            data-testid="column-type-select"
            value={typeDraft}
            onChange={(e) => setTypeDraft(e.target.value as ColumnType)}
          >
            {selectableColumnTypes.map((type) => (
              <option key={type.name} value={type.name}>
                {typeof type.presentation.label === 'string'
                  ? type.presentation.label
                  : type.name}
              </option>
            ))}
          </PanelSelect>
          {selectedTypeDescription && (
            <span className="field-help" data-testid="column-type-description">
              {selectedTypeDescription}
            </span>
          )}
        </label>
        <label className="field-group">
          <span className="field-labels">Display format</span>
          <PanelSelect
            className="form-input row-height-select"
            data-testid="column-format-select"
            value={formatValue}
            onChange={(e) => setFormatDraft(e.target.value as ColumnDisplayFormat | '')}
          >
            <option value="">Default</option>
            {allowedFormats.map((format) => (
              <option key={format.value} value={format.value}>{format.label}</option>
            ))}
          </PanelSelect>
        </label>
        <button
          type="button"
          className="btn column-save"
          data-testid="column-save-button"
          disabled={visibleUpdateState.busy || !hasChanges}
          onClick={() => { void saveColumn(); }}
        >
          <Save size={13} /> {visibleUpdateState.busy ? 'Saving...' : 'Save column'}
        </button>
      </div>
      {visibleUpdateState.error && (
        <div className="form-error" data-testid="column-update-error">{visibleUpdateState.error}</div>
      )}
      {visibleUpdateState.message && (
        <div className="column-update-result" data-testid="column-update-result">
          {visibleUpdateState.message}
        </div>
      )}
    </section>
  );
}

function reviewPassRateText(score: ReviewPassRate, subject: 'reviewed' | 'judged'): string {
  if (score.graded === 0) return 'Not reviewed';
  return `${score.passed}/${score.graded} ${subject} passed (${Math.round((score.passed / score.graded) * 100)}%)`;
}

function JudgeReviewScore({ score }: { score: JudgeReviewPassRate }) {
  const identity = `Judge run #${score.runId}${score.model ? ` · ${score.model}` : ''}`;
  return (
    <div className="run-review-score" data-testid={`judge-review-score-${score.runId}`}>
      <span className="prov-key">{identity}</span>
      <span>{reviewPassRateText(score, 'judged')}</span>
      {score.disagreementCount !== null && (
        <span className="muted" data-testid={`judge-disagreement-${score.runId}`}>
          {score.disagreementCount.toLocaleString()} disagreements across{' '}
          {score.compared.toLocaleString()} matched reviewed cells
        </span>
      )}
    </div>
  );
}

function RunReviewScores({ run }: { run: ColumnRun }) {
  return (
    <section className="run-review-scores" data-testid="run-review-scores">
      <h3 className="drawer-section-title">Review</h3>
      <div className="run-review-score" data-testid="human-review-score">
        <span className="prov-key">Human review</span>
        <span>{reviewPassRateText(run.humanScore, 'reviewed')}</span>
      </div>
      {run.judgeScores.length > 0 ? (
        <div className="run-review-judge-list">
          {run.judgeScores.map((score) => <JudgeReviewScore key={score.runId} score={score} />)}
        </div>
      ) : (
        <div className="run-review-score">
          <span className="prov-key">Judge review</span>
          <span>Not reviewed</span>
        </div>
      )}
    </section>
  );
}

function AiColumnDetails({
  column,
  sheetId,
  onBackfillComplete,
  requestCostConfirmation,
  onReviewRun,
}: {
  column: ColumnDef;
  sheetId: string;
  onBackfillComplete(runId: string, filled: number): void;
  requestCostConfirmation(estimate: RunEstimate, message: string): Promise<boolean>;
  onReviewRun(runId: string): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const ai = column.ai;
  const [loaded, setLoaded] = useState<LoadedColumnRuns | null>(null);
  const [loadingPage, setLoadingPage] = useState<'older' | 'newer' | null>(null);
  const mountedRef = useRef(false);
  const pageRequestRef = useRef(0);
  const [backfill, setBackfill] = useState<{
    busy: boolean;
    message: string | null;
    error: string | null;
  }>({ busy: false, message: null, error: null });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    const requestId = pageRequestRef.current + 1;
    pageRequestRef.current = requestId;
    void fetchColumnRuns(projectApi, column.id).then((result) => {
      if (alive && pageRequestRef.current === requestId) setLoaded(result);
    });
    return () => { alive = false; };
  }, [column.id]);

  if (!ai) return null;

  const info = loaded?.id === column.id ? loaded.info ?? null : null;
  const error = loaded?.id === column.id ? loaded.error ?? null : null;
  const runs = info?.runs ?? [];
  const current = info?.latestRun ?? info?.currentRun ?? runs.find((r) => r.current) ?? runs[0];
  const prompt = current ? specPrompt(current.spec) : ai.prompt ?? '';
  const fields = current ? specFields(current.spec) : [];
  const targetLanguage = current?.spec.target_language;
  const pageLimit = COLUMN_RUN_HISTORY_PAGE_SIZE;
  const olderOffset = info?.nextOffset ?? null;
  const newerOffset = info && info.offset > 0 ? Math.max(0, info.offset - pageLimit) : null;
  const pageStart = info && runs.length > 0 ? info.offset + 1 : 0;
  const pageEnd = info ? Math.min(info.offset + runs.length, info.totalRuns) : runs.length;
  const showPageState = Boolean(info && (info.offset > 0 || info.totalRuns > runs.length || info.hasMore));

  const loadRunsPage = (offset: number, direction: 'older' | 'newer') => {
    if (loadingPage !== null) return;
    const requestId = pageRequestRef.current + 1;
    pageRequestRef.current = requestId;
    setLoadingPage(direction);
    void fetchColumnRuns(projectApi, column.id, offset, pageLimit)
      .then((result) => {
        if (mountedRef.current && pageRequestRef.current === requestId) setLoaded(result);
      })
      .finally(() => {
        if (mountedRef.current && pageRequestRef.current === requestId) setLoadingPage(null);
      });
  };

  const runBackfill = async () => {
    setBackfill({ busy: true, message: null, error: null });
    try {
      // Over-gate backfills 402 and prompt via the shared
      // cost-confirmation modal; a null result is a user-cancelled gate — clear
      // the busy state with no mutation, no message, and no error.
      const result = await backfillWithConfirmation(
        projectApi,
        requestCostConfirmation,
        sheetId,
        column.name,
      );
      if (!result) {
        setBackfill({ busy: false, message: null, error: null });
        return;
      }
      setBackfill({
        busy: false,
        message: `Filled ${result.filled.toLocaleString()} ${result.filled === 1 ? 'cell' : 'cells'}.`,
        error: null,
      });
      void fetchColumnRuns(projectApi, column.id).then(setLoaded);
      onBackfillComplete(result.runId, result.filled);
    } catch (e: unknown) {
      setBackfill({
        busy: false,
        message: null,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  };

  return (
    <>
      <section className="col-meta">
        <div className="col-meta-row">
          <span className="prov-key">action</span>
          <span>{current?.actionName ?? ai.actionName}</span>
        </div>
        <div className="col-meta-row">
          <span className="prov-key">model</span>
          <span>
            {current?.model ||
              ai.model ||
              (current && LOCAL_RUN_KINDS.has(current.actionKind) ? 'local · no model' : '—')}
          </span>
        </div>
        {current && (
          <div className="col-meta-row">
            <span className="prov-key">latency</span>
            <span>{formatDuration(current.durationMs)}</span>
          </div>
        )}
        {current && (current.tokensIn != null || current.tokensOut != null) && (
          <div className="col-meta-row">
            <span className="prov-key">tokens</span>
            <span>
              {formatTokens(current.tokensIn)} in · {formatTokens(current.tokensOut)} out
            </span>
          </div>
        )}
        {current && (
          <div className="col-meta-row">
            <span className="prov-key">rows</span>
            <span>
              {current.completedRows.toLocaleString()}/{current.totalRows.toLocaleString()}
              {current.failedRows > 0 && (
                <span className="run-failed"> · {current.failedRows} failed</span>
              )}
            </span>
          </div>
        )}
        {current && (
          <div className="col-meta-row col-meta-action">
            <span className="prov-key">review</span>
            <span>
              <button
                type="button"
                className="mini-btn"
                data-testid="review-run-button"
                onClick={() => onReviewRun(current.runId)}
              >
                Review this run
              </button>
            </span>
          </div>
        )}
        {typeof targetLanguage === 'string' && (
          <div className="col-meta-row">
            <span className="prov-key">language</span>
            <span>{targetLanguage}</span>
          </div>
        )}
        <div className="col-meta-row">
          <span className="prov-key">latest run cost</span>
          <span>{formatUsdOrNone(current ? current.cost : ai.costSoFar)}</span>
        </div>
        <div className="col-meta-row">
          <span className="prov-key">output type</span>
          <span>{column.type}</span>
        </div>
        <div className="col-meta-row col-meta-action">
          <span className="prov-key">backfill</span>
          <span>
            <button
              type="button"
              className="mini-btn"
              data-testid="backfill-column-button"
              disabled={backfill.busy}
              onClick={() => { void runBackfill(); }}
            >
              <RefreshCcw size={11} /> {backfill.busy ? 'Backfilling...' : 'Run missing cells'}
            </button>
          </span>
        </div>
        {(backfill.message || backfill.error) && (
          <div
            className={`col-backfill-note${backfill.error ? ' col-backfill-error' : ''}`}
            data-testid="backfill-result"
          >
            {backfill.error ?? backfill.message}
          </div>
        )}
        {current && current.failedRows > 0 && (
          <div className="col-meta-row">
            <span className="prov-key">failed rows</span>
            <span className="run-failed">{current.failedRows.toLocaleString()}</span>
          </div>
        )}
      </section>

      {current && <RunReviewScores run={current} />}

      <section>
        <h3 className="drawer-section-title">Prompt</h3>
        {prompt ? (
          <pre className="col-prompt" data-testid="column-prompt">{prompt}</pre>
        ) : error ? (
          <div className="row-field-empty">prompt unavailable ({error})</div>
        ) : info ? (
          <div className="row-field-empty">no prompt recorded for this column</div>
        ) : (
          <PanelLoading label="loading…" />
        )}
      </section>

      {fields.length > 0 && (
        <section>
          <h3 className="drawer-section-title">Output fields</h3>
          <ul className="col-fields" data-testid="column-fields">
            {fields.map((f) => (
              <li key={f.name}>
                <span className="col-field-name">{f.name}</span>
                <span className="col-field-type">{f.type}</span>
                {f.description && <span className="col-field-desc">{f.description}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h3 className="drawer-section-title">Version history</h3>
        {!info && !error ? (
          <PanelLoading label="loading…" />
        ) : runs.length === 0 ? (
          <div className="row-field-empty">No runs yet — this column is waiting for its first run.</div>
        ) : (
          <>
            <ol className="version-list" data-testid="column-versions">
              {runs.map((r, i) => (
                <VersionItem
                  key={r.runId}
                  run={r}
                  version={(info?.totalRuns ?? runs.length) - (info?.offset ?? 0) - i}
                />
              ))}
            </ol>
            {showPageState && info && (
              <div className="version-page-state" data-testid="column-versions-page-note">
                Showing {pageStart.toLocaleString()}-{pageEnd.toLocaleString()} of{' '}
                {info.totalRuns.toLocaleString()} runs.
              </div>
            )}
            {(newerOffset !== null || olderOffset !== null) && (
              <div className="version-page-controls">
                {newerOffset !== null && (
                  <button
                    type="button"
                    className="history-page-button"
                    data-testid="column-versions-load-newer"
                    disabled={loadingPage !== null}
                    onClick={() => loadRunsPage(newerOffset, 'newer')}
                  >
                    <ChevronUp size={12} aria-hidden />
                    {loadingPage === 'newer' ? 'Loading' : 'Newer'}
                  </button>
                )}
                {olderOffset !== null && (
                  <button
                    type="button"
                    className="history-page-button"
                    data-testid="column-versions-load-older"
                    disabled={loadingPage !== null}
                    onClick={() => loadRunsPage(olderOffset, 'older')}
                  >
                    <ChevronDown size={12} aria-hidden />
                    {loadingPage === 'older' ? 'Loading' : 'Older'}
                  </button>
                )}
              </div>
            )}
            {info?.hasMore && olderOffset === null && (
              <div className="row-field-empty" data-testid="column-versions-truncated">
                Older versions are not loaded.
              </div>
            )}
          </>
        )}
      </section>
    </>
  );
}

function VersionItem({ run, version }: { run: ColumnRun; version: number }) {
  const when = run.startedAt
    ? new Date(run.startedAt).toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
      })
    : '—';
  return (
    <li className={`version-item${run.current ? ' version-current' : ''}`}>
      <div className="version-line">
        <span className="version-tag">v{version}</span>
        <span className={`version-status status-${run.status}`}>{run.status}</span>
        {run.current && <span className="history-pointer">current</span>}
        <span className="muted">{formatUsdOrNone(run.cost)}</span>
      </div>
      <div className="version-sub">
        {run.model || run.actionKind} · {run.completedRows.toLocaleString()}/{run.totalRows.toLocaleString()} rows
        {run.failedRows > 0 && (
          <span className="version-failed"> · {run.failedRows.toLocaleString()} failed</span>
        )}
        {run.durationMs != null && ` · ${formatDuration(run.durationMs)}`} · {when}
      </div>
    </li>
  );
}
