import { useEffect, useRef, useState } from 'react';
import type { AskCitation, AskEvent } from '../../api/projectQA';
import { isGeneratedActionDraft, type GeneratedActionDraft } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { MarkdownView } from '../../markdown';
import { AskToolActivity } from './AskToolActivity';

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
      setSource(citation); onOpenSource(citation);
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
  if (event.kind === 'action_proposal') {
    const proposal = payload.proposal;
    if (!proposal || typeof proposal !== 'object' || Array.isArray(proposal)) return null;
    const spec = proposal.spec;
    if (!spec || typeof spec !== 'object' || Array.isArray(spec) || !isGeneratedActionDraft(spec)) return null;
    const title = String(proposal.title ?? 'Suggested action');
    return <div className="ask-proposal"><strong>{title}</strong><p>Review the settings before running this action.</p><button type="button" className="btn" onClick={() => onInspectProposal(title, spec)}>Open action</button></div>;
  }
  if (event.kind === 'result_suggestion') return <div className="ask-result"><strong>{String(payload.title ?? 'Matching records')}</strong><p>{typeof payload.total === 'number' ? `${payload.total.toLocaleString()} matching rows` : 'Search results'}</p><button type="button" className="btn" onClick={() => typeof payload.citation_id === 'string' && openSource(payload.citation_id)}>Open results</button>{source?.message && <p>{source.message}</p>}{sourceError && <p role="alert">{sourceError}</p>}</div>;
  if (event.kind === 'question') return <div className="ask-question">{String(payload.question ?? '')}</div>;
  if (event.kind === 'answer' || event.kind === 'assistant') return <div className="ask-answer">
    <div ref={textRef}><MarkdownView source={String(payload.text ?? '')} /></div>
    {Array.isArray(payload.citation_ids) && <div className="ask-citations">{payload.citation_ids.filter((id): id is string => typeof id === 'string').map((id, index) =>
      <button type="button" key={id} aria-pressed={source?.id === id} onClick={() => openSource(id)}>{event.citations?.find((citation) => citation.id === id)?.label ?? `Source ${index + 1}`}</button>
    )}</div>}
    {sourceError && <p role="alert">{sourceError}</p>}
    {source && <div className="ask-source-preview"><strong>{source.label}</strong><button type="button" aria-label="Close source preview" onClick={() => setSource(null)}>×</button>{source.message && <p>{source.message}</p>}<p>{source.excerpt}</p>{source.target?.kind === 'web' && <>
      <p>{source.target.fetched ? 'Page read' : 'Search snippet'} · {new Date(source.target.retrieved_at).toLocaleString()}</p>
      <p><a href={source.target.url} target="_blank" rel="noopener noreferrer">{source.target.url}</a></p>
      <button type="button" className="btn" onClick={() => { if (source.target?.kind === 'web') onInspectProposal('Save web source', { action_id: 'import.urls', scope: { kind: 'project' }, params: { urls: [source.target.url] }, sheet_name: 'Web sources', output_names: {} }); }}>Save to project</button>
    </>}</div>}
    {event.kind === 'answer' && <div className="ask-answer-actions"><button type="button" onClick={() => void copy(false)}>Copy text</button><button type="button" onClick={() => void copy(true)}>Copy with sources</button>{copyStatus && <span role="status">{copyStatus}</span>}</div>}
  </div>;
  if (event.kind === 'tool_started' || event.kind === 'tool_completed') return <AskToolActivity event={event} />;
  if (event.kind === 'status' && ['stopped', 'interrupted', 'failed'].includes(String(payload.status))) return (
    <p className="ask-status">{payload.status === 'stopped' ? 'Stopped. The work above is saved.' : payload.status === 'interrupted' ? 'Interrupted. Send a follow-up to continue.' : String(payload.error_summary ?? 'This question could not be completed.')}</p>
  );
  return null;
}
