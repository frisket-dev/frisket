import type { RunTraceRow, RunTraceRowEvidence } from '../../api/types';
import { Button } from '../Button';
import { CopyButton } from '../CopyButton';
import { Modal } from '../Modal';
import { safeHttpUrl } from '../rowInspection/ValueLinks';
import './ResearchDetails.css';

type ResearchStep = NonNullable<RunTraceRow['research']>['steps'][number];

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function text(value: unknown): string {
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2) ?? ''; } catch { return '[Content unavailable]'; }
}

function duration(value: unknown): string | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? `${value.toLocaleString()} ms` : null;
}

function SourceLink({ value }: { value: string }) {
  const href = safeHttpUrl(value);
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer">{value}</a>
    : <span>{value}</span>;
}

/** Companion source values are supporting material, never review decisions. */
export function SourceLinks({ value }: { value: unknown }) {
  // CellValue's display boundary serializes non-scalar JSON cells as text.
  if (typeof value === 'string' && value.trimStart().startsWith('[')) {
    try {
      const parsed: unknown = JSON.parse(value);
      if (Array.isArray(parsed)) value = parsed;
    } catch { /* Plain source text remains readable. */ }
  }
  const values = Array.isArray(value) ? value : typeof value === 'string' ? value.split(/\r?\n/).filter(line => line.trim()) : value == null ? [] : [value];
  if (!values.length) return <p className="research-note">No source links recorded.</p>;
  return <ul className="research-sources">{values.map((item, index) => <li key={index}>
    {typeof item === 'string' ? <SourceLink value={item} /> : text(item)}
  </li>)}</ul>;
}

function CapturedText({ label, value }: { label: string; value: unknown }) {
  if (value == null) return null;
  const content = text(value);
  return <div className="research-captured">
    <div className="research-content-heading"><h4>{label}</h4><CopyButton text={content} label={`Copy ${label.toLowerCase()}`} /></div>
    <pre tabIndex={0} aria-label={label}>{content}</pre>
  </div>;
}

/** Generic Explain can use this without acquiring research-specific UI. */
export function ModelCallDetails({ calls }: { calls?: unknown[] }) {
  return <details className="research-disclosure">
    <summary>Model calls{calls ? ` (${calls.length})` : ''}</summary>
    {!calls?.length && <p className="research-note">Individual model calls were not recorded.</p>}
    {calls?.map((value, index) => {
      const call = object(value);
      if (!call) return <CapturedText key={index} label={`Call ${index + 1}`} value={value} />;
      const tokens = [typeof call.tokens_in === 'number' ? `${call.tokens_in.toLocaleString()} input tokens` : null,
        typeof call.tokens_out === 'number' ? `${call.tokens_out.toLocaleString()} output tokens` : null,
        duration(call.latency_ms)].filter(Boolean).join(' · ');
      return <details className="research-disclosure research-call" key={index}>
        <summary>Call {index + 1}{typeof call.model === 'string' ? ` · ${call.model}` : ''}{call.error ? ' · Error' : ''}</summary>
        {tokens && <p className="research-note">{tokens}</p>}
        {call.truncated === true && <p className="research-note">This model call was truncated; some content is unavailable.</p>}
        {call.request != null && <p className="research-note">Provider-neutral request as recorded.</p>}
        <CapturedText label={call.request != null ? 'Request' : 'Recorded prompt'} value={call.request ?? call.prompt} />
        <CapturedText label="Response" value={call.raw_response} />
        <CapturedText label="Tool calls" value={call.tool_calls} />
        <CapturedText label="Parsed response" value={call.data} />
        <CapturedText label="Error" value={call.error} />
        <CapturedText label="Recorded events" value={call.events} />
      </details>;
    })}
  </details>;
}

export function ResearchActivitySummary({ evidence }: { evidence: RunTraceRowEvidence }) {
  const row = evidence.trace?.row;
  const research = row?.research;
  const searches = research?.steps.filter(step => step.tool === 'search');
  const pages = research?.steps.filter(step => step.tool === 'fetch' && step.status === 'ok');
  const services = [...new Set(searches?.flatMap(step => [step.provider, step.service].filter((value): value is string => Boolean(value))) ?? [])];
  return <div className="research-activity-summary">
    <p>{row?.calls ? `${row.calls.length} model call${row.calls.length === 1 ? '' : 's'} recorded` : 'Model call count not recorded'}
      {research && ` · ${searches?.length ?? 0} search${searches?.length === 1 ? '' : 'es'} · ${pages?.length ?? 0} page${pages?.length === 1 ? '' : 's'} read`}
      {services.length > 0 && ` · Search service: ${services.join(', ')}`}</p>
    {!research && <p className="research-note">{row?.researchIncomplete ? 'Recorded tool activity is unavailable.' : 'Tool activity was not recorded for this run.'}</p>}
    {row?.researchIncomplete && <p className="research-note">Some recorded tool activity could not be read. Available activity may be incomplete.</p>}
    {(row?.truncated || research?.truncated) && <p className="research-note">Recorded details were truncated; some content is unavailable.</p>}
  </div>;
}

function ToolStep({ step, index }: { step: ResearchStep; index: number }) {
  const results = step.tool === 'search' ? step.results ?? [] : [];
  const status = { ok: 'Succeeded', error: 'Error', cancelled: 'Cancelled', started: 'Started (completion not recorded)' }[step.status];
  return <li className="research-step">
    <h4>{index + 1}. {step.tool === 'search' ? 'Search' : 'Read page'} · {status}</h4>
    {step.tool === 'search' ? <>
      <p className="research-query">{step.query}</p>
      <p className="research-note">Provider: {step.provider || 'not recorded'} · Service: {step.service || 'not recorded'}</p>
    </> : <p><SourceLink value={step.url} /></p>}
    {duration(step.duration_ms) && <p className="research-note">{duration(step.duration_ms)}</p>}
    {step.error_code && <p className="research-note">Error code: {step.error_code}</p>}
    {step.error && <p className="research-error">{step.error}</p>}
    {step.truncated && <p className="research-note">This tool result was truncated; some content is unavailable.</p>}
    {step.tool === 'search' ? <details className="research-disclosure">
      <summary>Returned results ({results.length})</summary>
      {results.length === 0 && <p className="research-note">No results recorded.</p>}
      <ul className="research-results">{results.map((result, resultIndex) => <li key={resultIndex}>
        {result.title && <p>{result.title}</p>}
        <SourceLink value={result.url} />
        {result.snippet && <p className="research-snippet">{result.snippet}</p>}
      </li>)}</ul>
    </details> : step.text != null && <details className="research-disclosure">
      <summary>Returned page text</summary><CapturedText label="Page text" value={step.text} />
    </details>}
  </li>;
}

/** Mount in a stable detail/row owner, outside hover-only cell controls. */
export function ResearchDetails({ evidence, sources, onClose }: {
  evidence: RunTraceRowEvidence; sources?: unknown; onClose: () => void;
}) {
  const row = evidence.trace?.row;
  const cell = object(evidence.cell);
  const error = cell?.error ?? row?.error;
  const runStatus = evidence.run?.status;
  const status = error ? 'Failed' : cell?.value != null ? 'Finished' : runStatus === 'cancelled' || runStatus === 'canceled' ? 'Cancelled'
    : runStatus === 'completed' || runStatus === 'finished' || runStatus === 'succeeded' ? 'Finished'
      : runStatus === 'failed' ? 'Failed' : runStatus || 'Execution status not recorded';
  const model = evidence.trace?.model ?? evidence.run?.model;
  return <Modal title="Research details" size="wide" onClose={onClose} testId="research-details">
    <div className="research-details">
      <p>{status}{model ? ` · ${model}` : ''}{duration(row?.latencyMs) ? ` · ${duration(row?.latencyMs)}` : ''}</p>
      <CapturedText label="Execution error" value={error} />
      <ResearchActivitySummary evidence={evidence} />
      {row?.research && <section aria-label="Search and page activity">
        <h3>Search and page activity</h3>
        {row.research.steps.length === 0 ? <p className="research-note">{row.researchIncomplete || row.research.truncated ? 'No tool steps are available in the recorded details.' : 'No research tools were used.'}</p>
          : <ol className="research-steps">{row.research.steps.map((step, index) => <ToolStep key={index} step={step} index={index} />)}</ol>}
      </section>}
      <section aria-label="Final recorded answer">
        <h3>Final recorded answer</h3>
        {cell && cell.value != null ? <CapturedText label="Answer" value={cell.value} /> : <p className="research-note">No final answer recorded for this run.</p>}
      </section>
      {sources !== undefined && <section aria-label="Sources encountered"><h3>Sources encountered</h3><SourceLinks value={sources} /></section>}
      <ModelCallDetails calls={row?.calls} />
      {!row?.calls?.length && row?.prompt != null && <details className="research-disclosure">
        <summary>Recorded model summary</summary>
        <CapturedText label="Recorded prompt" value={row.prompt} /><CapturedText label="Recorded response" value={row.rawResponse} />
      </details>}
      <div className="form-actions"><Button type="button" data-modal-autofocus onClick={onClose}>Close</Button></div>
    </div>
  </Modal>;
}
