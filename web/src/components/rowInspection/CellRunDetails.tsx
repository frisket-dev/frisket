import '../PanelPrimitives.css';
import './CellRunDetails.css';
import '../RowDrawer.css';
import { useCallback, useRef, useState, type ReactNode } from 'react';
import { Check, ChevronDown, ChevronRight, Copy, FileSearch2, X } from 'lucide-react';
import type { CellProvenance, RunTraceRowEvidence } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { confClass, formatUsdOrNone } from '../../format';
import { formatUsd } from '../../actions/model';
import { useAnchoredPosition } from '../../hooks/useAnchoredPosition';
import { useNativePopover } from '../../hooks/useNativePopover';
import { Button } from '../Button';

// Run traces stay plain text: recorded prompts and model outputs must never
// become rich HTML. Provenance disclosure is controlled by the row owner.
/** Flatten a recorded prompt (usually [{role, content}…]) to readable text. */
function formatPrompt(p: unknown): string | null {
  if (p == null) return null;
  if (typeof p === 'string') return p;
  if (Array.isArray(p)) {
    return p
      .map((m) => {
        if (m !== null && typeof m === 'object' && 'role' in m) {
          const msg = m as { role?: unknown; content?: unknown };
          const content =
            typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2);
          return `[${String(msg.role ?? 'message')}]\n${content}`;
        }
        return typeof m === 'string' ? m : JSON.stringify(m, null, 2);
      })
      .join('\n\n');
  }
  return JSON.stringify(p, null, 2);
}

function CopyButton({ text, testId }: { text: string; testId: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"

      data-testid={testId}
      title="Copy to clipboard"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      {copied ? <Check size={11} /> : <Copy size={11} />} {copied ? 'copied' : 'copy'}
    </Button>
  );
}

type ExplainState =
  | { phase: 'idle' | 'loading' | 'error' }
  | { phase: 'done'; evidence: RunTraceRowEvidence };

export function ExplainBlock({
  runId,
  rowId,
  columnId,
  compact = false,
  columnName,
  testId,
}: {
  runId: string;
  rowId: string;
  columnId?: string | null;
  compact?: boolean;
  columnName?: string;
  testId?: string;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<ExplainState>({ phase: 'idle' });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const label = `Explain ${columnName ?? 'this'} cell`;
  const description = 'the exact prompt and raw model output that produced this value';

  // Fetch lazily, on first expand only (event handler, not an effect).
  const toggle = () => {
    setOpen((o) => !o);
    if (state.phase !== 'idle') return;
    setState({ phase: 'loading' });
    api
      .getRunTraceRow(runId, rowId, columnId)
      .then((evidence) => setState({ phase: 'done', evidence }))
      .catch(() => setState({ phase: 'error' }));
  };
  const close = useCallback(() => setOpen(false), []);

  // Viewport-clamped + flip-above placement anchored to the trigger — the
  // same primitive EnginePicker/ModelPicker/MultiColumnPicker use, rather
  // than the panel's old inline `margin-top` flow (it rendered as a flex
  // child of the 20px action slot, so a wide trace panel had nowhere to grow
  // but off the edge of the screen).
  const panelPos = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: compact ? 'right' : 'left',
    width: (_rect, viewportWidth) => Math.min(380, viewportWidth - 16),
    gap: 6,
    minHeight: 160,
  });

  // Native top-layer popover (see useNativePopover's doc comment): this is
  // what lets InspectDetailColumn's own useEscapeDismiss defer to Escape
  // here instead of closing the whole Detail column out from under it.
  // `outside: false` is deliberate — the owner wants EXPLICIT dismissal
  // only (the panel's own × button, Escape, or closing the Detail column
  // wholesale by unmounting this component), not a stray click elsewhere,
  // and definitely not the old "any keypress closes it" behavior (there was
  // no scoped Escape handling before, so a bug elsewhere on the page could
  // yank focus and read as "dismissed on typing" — this hook only ever
  // wires up Escape).
  useNativePopover(panelRef, close, { enabled: open, outside: false });

  return (
    <div className={compact ? 'explain explain-compact' : 'explain'}>
      <Button
        type="button"
        ref={triggerRef}
        className={compact ? 'row-field-icon explain-toggle' : 'btn explain-toggle'}
        data-testid={testId ?? (compact ? 'cell-action-explain' : 'explain-cell')}
        aria-label={compact ? label : undefined}
        title={compact ? `${label} — view ${description}` : `View ${description}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={toggle}
      >
        <FileSearch2 size={12} /> {compact ? null : 'Explain this cell'}
      </Button>
      {open && (
        <div
          ref={panelRef}
          role="dialog"
          aria-label={label}
          className="explain-panel"
          data-testid="explain-panel"
          style={
            panelPos
              ? {
                  top: panelPos.top,
                  bottom: panelPos.bottom,
                  left: panelPos.left,
                  width: panelPos.width,
                  maxHeight: panelPos.maxHeight,
                }
              : { visibility: 'hidden' }
          }
        >
          <div className="explain-panel-head">
            <span className="explain-panel-title">Explain this cell</span>
            <button
              type="button"
              className="row-field-icon explain-close"
              data-testid="explain-panel-close"
              aria-label="Close explain panel"
              title="Close"
              onClick={close}
            >
              <X size={12} />
            </button>
          </div>
          {state.phase === 'loading' && <div className="explain-note">loading trace…</div>}
          {state.phase === 'error' && (
            <div className="explain-note">Could not load the trace for this run.</div>
          )}
          {state.phase === 'done' && <ExplainTraceRow evidence={state.evidence} />}
        </div>
      )}
    </div>
  );
}

function formatRunDuration(startedAt: string | null, finishedAt: string | null): string | null {
  if (!startedAt || !finishedAt) return null;
  const ms = Date.parse(finishedAt) - Date.parse(startedAt);
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function ExplainTraceRow({ evidence }: { evidence: RunTraceRowEvidence }) {
  const trace = evidence.trace;
  const rec = trace?.row ?? null;
  if (!rec) {
    // No per-row model trace. For a deterministic (non-LLM) op there is no
    // prompt/output to show, but the run-level facts ARE available — surface
    // them instead of dead-ending on an unavailable trace.
    const run = evidence.run ?? null;
    const duration = run ? formatRunDuration(run.startedAt, run.finishedAt) : null;
    return (
      <div
        data-testid={evidence.status === 'not_recorded' ? 'explain-empty' : 'explain-missing-row'}
      >
        {run && (
          <div className="provenance-grid explain-meta" data-testid="explain-run-facts">
            {run.actionName && (
              <>
                <span className="prov-key">action</span>
                <span>{run.actionName}</span>
              </>
            )}
            {run.model && (
              <>
                <span className="prov-key">engine</span>
                <span>{run.model}</span>
              </>
            )}
            {run.status && (
              <>
                <span className="prov-key">status</span>
                <span>{run.status}</span>
              </>
            )}
            {duration && (
              <>
                <span className="prov-key">duration</span>
                <span>{duration}</span>
              </>
            )}
            <span className="prov-key">cost</span>
            <span>{formatUsdOrNone(run.costActual)}</span>
          </div>
        )}
        <div className="explain-note">{runTraceAbsenceMessage(evidence.status)}</div>
      </div>
    );
  }
  const prompt = formatPrompt(rec.prompt);
  return (
    <>
      <div className="provenance-grid explain-meta" data-testid="explain-meta">
        {trace?.actionName && (
          <>
            <span className="prov-key">action</span>
            <span>{trace.actionName}</span>
          </>
        )}
        {trace?.model && (
          <>
            <span className="prov-key">model</span>
            <span>{trace.model}</span>
          </>
        )}
        <span className="prov-key">latency</span>
        <span>{rec.latencyMs != null ? `${rec.latencyMs.toLocaleString()} ms` : '—'}{rec.cached ? ' · cached' : ''}</span>
        <span className="prov-key">tokens</span>
        <span>
          {rec.tokensIn != null || rec.tokensOut != null
            ? `${rec.tokensIn ?? '—'} in / ${rec.tokensOut ?? '—'} out`
            : '—'}
        </span>
        {rec.retries > 0 && (
          <>
            <span className="prov-key">retries</span>
            <span>{rec.retries}</span>
          </>
        )}
        {rec.error && (
          <>
            <span className="prov-key">error</span>
            <span className="explain-error">{rec.error}</span>
          </>
        )}
      </div>

      <div className="explain-section-head">
        <span>Prompt</span>
        {prompt && <CopyButton text={prompt} testId="explain-copy-prompt" />}
      </div>
      {prompt ? (
        <pre className="explain-pre" data-testid="explain-prompt">{prompt}</pre>
      ) : (
        <div className="explain-note">No prompt captured — this op ran without a model call.</div>
      )}

      <div className="explain-section-head">
        <span>Raw model output</span>
        {rec.rawResponse && <CopyButton text={rec.rawResponse} testId="explain-copy-raw" />}
      </div>
      {rec.rawResponse ? (
        <pre className="explain-pre" data-testid="explain-raw">{rec.rawResponse}</pre>
      ) : (
        <div className="explain-note">No raw output captured for this row.</div>
      )}
    </>
  );
}

function runTraceAbsenceMessage(status: RunTraceRowEvidence['status']): ReactNode {
  switch (status) {
    case 'not_recorded':
      return 'Trace logging was unavailable or the trace was dropped for this run.';
    case 'row_not_in_run':
      return 'This row is not in the run trace. It may not have been part of the run.';
    case 'missing':
      return 'Trace evidence for this row is missing.';
    case 'recorded':
      return 'Trace evidence for this row was recorded, but no row payload is available.';
  }
}

export function ProvenanceBlock({
  prov,
  runResultExpanded,
  onToggleRunResult,
}: {
  prov: CellProvenance;
  runResultExpanded: boolean;
  onToggleRunResult(): void;
}) {
  const confidence =
    typeof prov.confidence === 'number' && Number.isFinite(prov.confidence)
      ? prov.confidence
      : null;
  const isRunResult = prov.currentValueRef.kind === 'run_result';
  return (
    <div className="provenance" data-testid="cell-provenance">
      <div className="provenance-grid">
        <span className="prov-key">origin</span>
        {isRunResult ? (
          <Button
            type="button"

            data-testid="cell-provenance-run-result-toggle"
            variant="text"
            aria-expanded={runResultExpanded}
            aria-label={`${runResultExpanded ? 'Hide' : 'Show'} run result details`}
            onClick={onToggleRunResult}
          >
            {runResultExpanded
              ? <ChevronDown size={12} aria-hidden="true" />
              : <ChevronRight size={12} aria-hidden="true" />}
            <span data-testid="cell-provenance-origin">run result</span>
          </Button>
        ) : (
          <span data-testid="cell-provenance-origin">
            {cellOriginLabel(prov.currentValueRef.kind)}
          </span>
        )}
        {isRunResult && runResultExpanded && (
          <>
            <span className="prov-key">action</span>
            <span>{prov.actionName}</span>
            <span className="prov-key">model</span>
            <span>{prov.model}</span>
            <span className="prov-key">cost</span>
            <span>{formatUsd(prov.cost)}</span>
            <span className="prov-key">confidence</span>
            <span>
              {confidence === null ? '—' : (
                <span className={`conf-pill ${confClass(confidence)}`}>{confidence.toFixed(2)}</span>
              )}
            </span>
          </>
        )}
        {prov.currentValueRef.kind === 'manual_edit' && prov.currentValueRef.opId && (
          <>
            <span className="prov-key">op</span>
            <span>{prov.currentValueRef.opId}</span>
          </>
        )}
        {prov.currentValueRef.kind === 'source_cell' && (
          <>
            <span className="prov-key">column</span>
            <span>{prov.currentValueRef.columnId}</span>
          </>
        )}
        {prov.currentValueRef.kind === 'missing' && (
          <>
            <span className="prov-key">state</span>
            <span>missing</span>
          </>
        )}
      </div>
      {prov.justification && (
        <p className="prov-justification" data-testid="prov-justification">{prov.justification}</p>
      )}
    </div>
  );
}

function cellOriginLabel(kind: CellProvenance['currentValueRef']['kind']): string {
  switch (kind) {
    case 'manual_edit':
      return 'manual edit';
    case 'run_result':
      return 'run result';
    case 'source_cell':
      return 'source cell';
    case 'compacted':
      return 'compacted evidence';
    case 'missing':
      return 'missing';
    default:
      return 'unknown';
  }
}
