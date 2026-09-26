import { useEffect, useRef, useState } from 'react';
import { ChartColumnIncreasing, ChevronRight, Copy, Files } from 'lucide-react';
import type { AskCitation, AskEvent } from '../../api/projectQA';
import { isGeneratedActionDraft, type GeneratedActionDraft } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { MarkdownView } from '../../markdown';
import { AskToolActivity } from './AskToolActivity';

function hasPreview(citation: AskCitation): boolean {
  return citation.target?.kind === 'web' || citation.status === 'unavailable' || citation.status === 'changed';
}

export function AskEventContent({ event, onInspectProposal, onOpenSource }: { event: AskEvent; onOpenSource(citation: AskCitation): void; onInspectProposal(title: string, spec: GeneratedActionDraft): void }) {
  const payload = event.payload;
  const { qa } = useWorkspaceStores();
  const [source, setSource] = useState<AskCitation | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const request = useRef(0);
  useEffect(() => () => { request.current += 1; }, []);
  const openSource = (id: string) => {
    const version = ++request.current;
    setSourceError(null);
    void qa.citation(event.thread_id, id).then((citation) => {
      if (version !== request.current) return;
      setSource(hasPreview(citation) ? citation : null); onOpenSource(citation);
    }).catch(() => { if (version === request.current) setSourceError('Could not open this source. Please try again.'); });
  };
  async function copy(withSources: boolean) {
    try {
      const text = textRef.current?.innerText ?? textRef.current?.textContent ?? String(payload.text ?? '');
      const ids = Array.isArray(payload.citation_ids) ? payload.citation_ids.filter((id): id is string => typeof id === 'string') : [];
      const sources = withSources ? await Promise.all(ids.map((id) => qa.citation(event.thread_id, id))) : [];
      const labels = sources.map((citation, index) => `[${index + 1}] ${citation.label}${citation.target?.kind === 'web' ? ` — ${citation.target.url}` : ''}`);
      await navigator.clipboard.writeText(text + (labels.length ? `\n\n${labels.join('\n')}` : ''));
      setCopyStatus('Copied');
    } catch { setCopyStatus('Could not copy. Please try again.'); }
  }
  async function copyDebug(text: string) {
    try { await navigator.clipboard.writeText(text); setCopyStatus('Copied'); }
    catch { setCopyStatus('Could not copy. Please try again.'); }
  }
  if (event.kind === 'action_proposal') {
    const proposal = payload.proposal;
    if (!proposal || typeof proposal !== 'object' || Array.isArray(proposal)) return null;
    const spec = proposal.spec;
    if (!spec || typeof spec !== 'object' || Array.isArray(spec) || !isGeneratedActionDraft(spec)) return null;
    const title = String(proposal.title ?? 'Suggested action');
    return <div className="ask-proposal"><strong>{title}</strong><p>Review the settings before running this action.</p><button type="button" className="btn" onClick={() => onInspectProposal(title, spec)}>Open action</button></div>;
  }
  if (event.kind === 'result_suggestion') {
    const title = String(payload.title ?? 'Matching records');
    const total = typeof payload.total === 'number' ? payload.total.toLocaleString() : null;
    const sheetName = typeof payload.sheet_name === 'string' && payload.sheet_name.trim() ? payload.sheet_name : title;
    const open = () => { if (typeof payload.citation_id === 'string') openSource(payload.citation_id); };
    return <div className="ask-analysis"><button type="button" className="ask-analysis-summary" onClick={open}><ChartColumnIncreasing size={15} aria-hidden /><span>{total ? `${total} ${Number(payload.total) === 1 ? 'row' : 'rows'} in ${sheetName}` : sheetName}</span><ChevronRight size={15} aria-hidden /></button>{source?.message && source.status !== 'current' && <p>{source.message}</p>}{sourceError && <p role="alert">{sourceError}</p>}</div>;
  }
  if (event.kind === 'question') return <div className="ask-question">{String(payload.question ?? '')}</div>;
  if (event.kind === 'answer' || event.kind === 'assistant') return <div className="ask-answer">
    <div ref={textRef}><MarkdownView source={String(payload.text ?? '')} /></div>
    {Array.isArray(payload.citation_ids) && <div className="ask-citations">{payload.citation_ids.filter((id): id is string => typeof id === 'string').map((id, index) =>
      <button type="button" key={id} aria-pressed={source?.id === id} onClick={() => openSource(id)}>{event.citations?.find((citation) => citation.id === id)?.label ?? `Source ${index + 1}`}</button>
    )}</div>}
    {sourceError && <p role="alert">{sourceError}</p>}
    {source && hasPreview(source) && <div className="ask-source-preview"><strong>{source.label}</strong><button type="button" aria-label="Close source preview" onClick={() => setSource(null)}>×</button>{source.message && <p>{source.message}</p>}{source.target?.kind === 'web' && <><p>{source.excerpt}</p>
      <p>{source.target.fetched ? 'Page read' : 'Search snippet'} · {new Date(source.target.retrieved_at).toLocaleString()}</p>
      <p><a href={source.target.url} target="_blank" rel="noopener noreferrer">{source.target.url}</a></p>
      <button type="button" className="btn" onClick={() => { if (source.target?.kind === 'web') onInspectProposal('Save web source', { action_id: 'import.urls', scope: { kind: 'project' }, params: { urls: [source.target.url] }, sheet_name: 'Web sources', output_names: {} }); }}>Save to project</button>
    </>}</div>}
    {event.kind === 'answer' && <div className="ask-answer-actions"><button type="button" aria-label="Copy answer text" title="Copy answer text" onClick={() => void copy(false)}><Copy size={14} aria-hidden /></button><button type="button" aria-label="Copy answer with sources" title="Copy answer with sources" onClick={() => void copy(true)}><Files size={14} aria-hidden /></button>{copyStatus && <span role="status">{copyStatus}</span>}</div>}
  </div>;
  if (event.kind === 'tool_started' || event.kind === 'tool_completed') return <AskToolActivity event={event} />;
  if (event.kind === 'status' && ['stopped', 'interrupted', 'failed'].includes(String(payload.status))) {
    const rawDiagnostic = payload.diagnostic;
    const diagnostic = rawDiagnostic !== null && typeof rawDiagnostic === 'object' && !Array.isArray(rawDiagnostic) ? rawDiagnostic as Record<string, unknown> : null;
    const code = diagnostic?.code === 'invalid_model_response' || diagnostic?.code === 'internal_error' ? diagnostic.code : null;
    const reference = typeof diagnostic?.reference === 'string' ? diagnostic.reference : null;
    const tool = typeof diagnostic?.tool === 'string' ? diagnostic.tool : null;
    const hasDetails = payload.status === 'failed' && !!code && !!reference;
    const debugText = [code && `Code: ${code}`, reference && `Reference: ${reference}`, tool && `Tool: ${tool}`].filter((line): line is string => !!line).join('\n');
    const errorSummary = typeof payload.error_summary === 'string' && payload.error_summary.trim() ? payload.error_summary : null;
    const summary = payload.status === 'stopped' ? 'Stopped. The work above is saved.' : payload.status === 'interrupted' ? 'Interrupted. Send a follow-up to continue.'
      : errorSummary ?? (code === 'invalid_model_response' ? 'The selected model returned an unusable response. Try a more specific question or another model.' : 'Ask could not complete this question. Please try again.');
    return <div className="ask-status ask-failure-status"><p>{summary}</p>{hasDetails && <details className="ask-failure-details"><summary>Details</summary><dl><div><dt>Code</dt><dd>{code}</dd></div><div><dt>Reference</dt><dd>{reference}</dd></div>{tool && <div><dt>Tool</dt><dd>{tool}</dd></div>}</dl><button type="button" aria-label="Copy failure details" title="Copy failure details" onClick={() => void copyDebug(debugText)}><Copy size={13} aria-hidden /></button>{copyStatus && <span role="status">{copyStatus}</span>}</details>}</div>;
  }
  return null;
}
