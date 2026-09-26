import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChartColumnIncreasing, ChevronRight, Copy, Files } from 'lucide-react';
import type { AskCitation, AskEvent } from '../../api/projectQA';
import type { ActionCatalogEntry, GeneratedActionDraft } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { MarkdownView } from '../../markdown';
import { ACTION_PLACEMENTS } from '../../actions/model';
import { ACTION_ICON_BY_KIND } from '../../workbench/actSurface';
import { askActionProposal, type AskActionProposal } from './actionProposal';

function hasPreview(citation: AskCitation): boolean {
  return citation.target?.kind === 'web' || citation.status === 'unavailable' || citation.status === 'changed';
}

function markerIndex(href: string, prefix: string): number | null {
  const match = new RegExp(`^#${prefix}([1-9]\\d*)$`).exec(href);
  if (!match) return null;
  const index = Number(match[1]);
  return Number.isSafeInteger(index) ? index : null;
}

function hasInlineMarker(text: string, prefix: string, valid: (index: number) => boolean): boolean {
  return Array.from(text.matchAll(new RegExp(`\\[[^\\]]+\\]\\(#${prefix}([1-9]\\d*)\\)`, 'g')))
    .some((match) => valid(Number(match[1])));
}

export function AskEventContent({ event, onInspectProposal, onOpenSource, actionProposals = [], actionCatalog = [], onOpenAction }: {
  event: AskEvent;
  onOpenSource(citation: AskCitation): void;
  onInspectProposal(title: string, spec: GeneratedActionDraft): void;
  actionProposals?: readonly AskActionProposal[];
  actionCatalog?: readonly ActionCatalogEntry[];
  onOpenAction?(actionId: string): void;
}) {
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
    const proposal = askActionProposal(event);
    if (!proposal) return null;
    return <div className="ask-proposal"><strong>{proposal.title}</strong><p>Review the settings before running this action.</p><button type="button" className="btn" onClick={() => onInspectProposal(proposal.title, proposal.spec)}>Open action</button></div>;
  }
  if (event.kind === 'result_suggestion') {
    const title = String(payload.title ?? 'Matching records');
    const total = typeof payload.total === 'number' ? payload.total.toLocaleString() : null;
    const sheetName = typeof payload.sheet_name === 'string' && payload.sheet_name.trim() ? payload.sheet_name : title;
    const open = () => { if (typeof payload.citation_id === 'string') openSource(payload.citation_id); };
    return <div className="ask-analysis"><button type="button" className="ask-analysis-summary" onClick={open}><ChartColumnIncreasing size={15} aria-hidden /><span>{total ? `${total} ${Number(payload.total) === 1 ? 'row' : 'rows'} in ${sheetName}` : sheetName}</span><ChevronRight size={15} aria-hidden /></button>{source?.message && source.status !== 'current' && <p>{source.message}</p>}{sourceError && <p role="alert">{sourceError}</p>}</div>;
  }
  if (event.kind === 'question') return <div className="ask-question">{String(payload.question ?? '')}</div>;
  if (event.kind === 'answer' || event.kind === 'assistant') {
    const text = String(payload.text ?? '');
    const citationIds = Array.isArray(payload.citation_ids) ? payload.citation_ids.filter((id): id is string => typeof id === 'string') : [];
    const proposalsBySeq = new Map(actionProposals.filter((proposal) => proposal.event.turn_id === event.turn_id).map((proposal) => [proposal.event.seq, proposal]));
    const catalogByKind = new Map(actionCatalog.map((entry) => [entry.kind, entry]));
    const hasInlineCitations = hasInlineMarker(text, 'cite-', (index) => index <= citationIds.length);
    const renderLink = (href: string, children: ReactNode) => {
      const citationIndex = markerIndex(href, 'cite-');
      if (citationIndex && citationIndex <= citationIds.length) {
        const id = citationIds[citationIndex - 1];
        const label = event.citations?.find((citation) => citation.id === id)?.label;
        return <button type="button" className="ask-inline-citation" aria-label={label ? `Source ${citationIndex}: ${label}` : `Source ${citationIndex}`} aria-pressed={source?.id === id} onClick={() => openSource(id)}>{children}</button>;
      }
      const proposal = markerIndex(href, 'action-');
      if (proposal) {
        const prepared = proposalsBySeq.get(proposal);
        if (prepared) {
          const Icon = ACTION_ICON_BY_KIND[prepared.spec.action_id];
          const placement = ACTION_PLACEMENTS[prepared.spec.action_id];
          const title = placement ? `${prepared.title} · ${placement.tab} / ${placement.group}` : prepared.title;
          return <button type="button" className="ask-inline-action" title={title} onClick={() => onInspectProposal(prepared.title, prepared.spec)}>{Icon && <Icon size={14} aria-hidden />}{prepared.title}</button>;
        }
      }
      const generic = /^#action\/([a-z][a-z0-9_.-]*)$/.exec(href)?.[1];
      const entry = generic ? catalogByKind.get(generic) : undefined;
      if (entry && onOpenAction) {
        const Icon = ACTION_ICON_BY_KIND[entry.kind];
        const placement = ACTION_PLACEMENTS[entry.kind];
        const title = placement ? `${entry.title} · ${placement.tab} / ${placement.group}` : entry.title;
        return <button type="button" className="ask-inline-action" title={title} onClick={() => onOpenAction(entry.kind)}>{Icon && <Icon size={14} aria-hidden />}{entry.title}</button>;
      }
      return undefined;
    };
    return <div className="ask-answer">
      <div ref={textRef}><MarkdownView source={text} renderLink={renderLink} /></div>
      {citationIds.length > 0 && !hasInlineCitations && <details className="ask-citation-sources"><summary>Sources</summary>{citationIds.map((id, index) =>
        <button type="button" key={`${id}:${index}`} aria-pressed={source?.id === id} onClick={() => openSource(id)}>[{index + 1}] {event.citations?.find((citation) => citation.id === id)?.label ?? `Source ${index + 1}`}</button>
      )}</details>}
    {sourceError && <p role="alert">{sourceError}</p>}
    {source && hasPreview(source) && <div className="ask-source-preview"><strong>{source.label}</strong><button type="button" aria-label="Close source preview" onClick={() => setSource(null)}>×</button>{source.message && <p>{source.message}</p>}{source.target?.kind === 'web' && <><p>{source.excerpt}</p>
      <p>{source.target.fetched ? 'Page read' : 'Search snippet'} · {new Date(source.target.retrieved_at).toLocaleString()}</p>
      <p><a href={source.target.url} target="_blank" rel="noopener noreferrer">{source.target.url}</a></p>
      <button type="button" className="btn" onClick={() => { if (source.target?.kind === 'web') onInspectProposal('Save web source', { action_id: 'import.urls', scope: { kind: 'project' }, params: { urls: [source.target.url] }, sheet_name: 'Web sources', output_names: {} }); }}>Save to project</button>
    </>}</div>}
    {event.kind === 'answer' && <div className="ask-answer-actions"><button type="button" aria-label="Copy answer text" title="Copy answer text" onClick={() => void copy(false)}><Copy size={14} aria-hidden /></button><button type="button" aria-label="Copy answer with sources" title="Copy answer with sources" onClick={() => void copy(true)}><Files size={14} aria-hidden /></button>{copyStatus && <span role="status">{copyStatus}</span>}</div>}
    </div>;
  }
  if (event.kind === 'status' && ['stopped', 'interrupted', 'failed'].includes(String(payload.status))) {
    const rawDiagnostic = payload.diagnostic;
    const diagnostic = rawDiagnostic !== null && typeof rawDiagnostic === 'object' && !Array.isArray(rawDiagnostic) ? rawDiagnostic as Record<string, unknown> : null;
    const code = diagnostic?.code === 'invalid_model_response' || diagnostic?.code === 'internal_error' ? diagnostic.code : null;
    const reasonLabels: Record<string, string> = { tool_call_invalid: 'Invalid tool arguments', citation_invalid: 'Invalid source citation', final_result_invalid: 'Invalid answer format', output_failure: 'Model output was not accepted' };
    const reason = typeof diagnostic?.reason === 'string' ? reasonLabels[diagnostic.reason] : undefined;
    const reference = typeof diagnostic?.reference === 'string' ? diagnostic.reference : null;
    const tool = typeof diagnostic?.tool === 'string' ? diagnostic.tool : null;
    const hasDetails = payload.status === 'failed' && !!code && !!reference;
    const debugText = [code && `Code: ${code}`, reason && `Reason: ${reason}`, reference && `Reference: ${reference}`, tool && `Tool: ${tool}`].filter((line): line is string => !!line).join('\n');
    const errorSummary = typeof payload.error_summary === 'string' && payload.error_summary.trim() ? payload.error_summary : null;
    const summary = payload.status === 'stopped' ? 'Stopped. The work above is saved.' : payload.status === 'interrupted' ? 'Interrupted. Send a follow-up to continue.'
      : errorSummary ?? (code === 'invalid_model_response' ? 'The selected model returned an unusable response. Try a more specific question or another model.' : 'Ask could not complete this question. Please try again.');
    return <div className="ask-status ask-failure-status"><p>{summary}</p>{hasDetails && <details className="ask-failure-details"><summary>Details</summary><dl><div><dt>Code</dt><dd>{code}</dd></div>{reason && <div><dt>Reason</dt><dd>{reason}</dd></div>}<div><dt>Reference</dt><dd>{reference}</dd></div>{tool && <div><dt>Tool</dt><dd>{tool}</dd></div>}</dl><button type="button" aria-label="Copy failure details" title="Copy failure details" onClick={() => void copyDebug(debugText)}><Copy size={13} aria-hidden /></button>{copyStatus && <span role="status">{copyStatus}</span>}</details>}</div>;
  }
  return null;
}
