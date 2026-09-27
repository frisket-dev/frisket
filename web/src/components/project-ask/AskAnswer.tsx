import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Copy, Files } from 'lucide-react';
import type { AskCitation, AskEvent } from '../../api/projectQA';
import type { ActionCatalogEntry, GeneratedActionDraft } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { MarkdownView } from '../../markdown';
import { ACTION_PLACEMENTS } from '../../actions/model';
import { ACTION_ICON_BY_KIND, baseActTabLabel } from '../../workbench/actSurface';
import { type AskActionProposal } from './actionProposal';
import { hasCitationPreview } from './citationPreview';
import { actionKind, hasInlineMarker, isReservedReference, markerIndex } from './inlineReferences';

function actionTooltip(kind: string, title: string): string {
  const tab = ACTION_PLACEMENTS[kind] && baseActTabLabel(ACTION_PLACEMENTS[kind].tab);
  return tab ? `${tab} tab > ${title}` : title;
}

export function AskAnswer({ event, onInspectProposal, onOpenSource, actionProposals = [], actionCatalog = [], onOpenAction }: {
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
  const text = String(payload.text ?? '');
  const citationIds = Array.isArray(payload.citation_ids) ? payload.citation_ids.filter((id): id is string => typeof id === 'string') : [];
  const proposalsBySeq = new Map(actionProposals.filter((proposal) => proposal.event.turn_id === event.turn_id).map((proposal) => [proposal.event.seq, proposal]));
  const catalogByKind = new Map(actionCatalog.map((entry) => [entry.kind, entry]));
  const hasInlineCitations = hasInlineMarker(text, 'cite-', (index) => index <= citationIds.length);
  useEffect(() => () => { request.current += 1; }, []);
  const openSource = (id: string) => {
    const version = ++request.current;
    setSourceError(null);
    void qa.citation(event.thread_id, id).then((citation) => {
      if (version !== request.current) return;
      setSource(hasCitationPreview(citation) ? citation : null); onOpenSource(citation);
    }).catch(() => { if (version === request.current) setSourceError('Could not open this source. Please try again.'); });
  };
  async function copy(withSources: boolean) {
    try {
      const answer = textRef.current?.innerText ?? textRef.current?.textContent ?? text;
      const sources = withSources ? await Promise.all(citationIds.map((id) => qa.citation(event.thread_id, id))) : [];
      const labels = sources.map((citation, index) => `[${index + 1}] ${citation.label}${citation.target?.kind === 'web' ? ` — ${citation.target.url}` : ''}`);
      await navigator.clipboard.writeText(answer + (labels.length ? `\n\n${labels.join('\n')}` : ''));
      setCopyStatus('Copied');
    } catch { setCopyStatus('Could not copy. Please try again.'); }
  }
  const renderLink = (href: string, children: ReactNode) => {
    const citationIndex = markerIndex(href, 'cite-');
    if (citationIndex && citationIndex <= citationIds.length) {
      const id = citationIds[citationIndex - 1];
      const label = event.citations?.find((citation) => citation.id === id)?.label;
      return <button type="button" className="ask-inline-citation" aria-label={label ? `Source ${citationIndex}: ${label}` : `Source ${citationIndex}`} aria-pressed={source?.id === id} onClick={() => openSource(id)}>{citationIndex}</button>;
    }
    const proposalIndex = markerIndex(href, 'action-');
    const prepared = proposalIndex ? proposalsBySeq.get(proposalIndex) : undefined;
    if (prepared) {
      const Icon = ACTION_ICON_BY_KIND[prepared.spec.action_id];
      const title = catalogByKind.get(prepared.spec.action_id)?.title ?? prepared.title;
      return <button type="button" className="ask-inline-action" title={actionTooltip(prepared.spec.action_id, title)} onClick={() => onInspectProposal(prepared.title, prepared.spec)}>{Icon && <Icon size={14} aria-hidden />}{prepared.title}</button>;
    }
    const kind = actionKind(href);
    const entry = kind ? catalogByKind.get(kind) : undefined;
    if (entry && onOpenAction) {
      const Icon = ACTION_ICON_BY_KIND[entry.kind];
      return <button type="button" className="ask-inline-action" title={actionTooltip(entry.kind, entry.title)} onClick={() => onOpenAction(entry.kind)}>{Icon && <Icon size={14} aria-hidden />}{entry.title}</button>;
    }
    return isReservedReference(href) ? children : undefined;
  };
  return <div className="ask-answer">
    <div ref={textRef}><MarkdownView source={text} renderLink={renderLink} /></div>
    {citationIds.length > 0 && !hasInlineCitations && <details className="ask-citation-sources"><summary>Sources</summary>{citationIds.map((id, index) =>
      <button type="button" key={`${id}:${index}`} aria-pressed={source?.id === id} onClick={() => openSource(id)}>[{index + 1}] {event.citations?.find((citation) => citation.id === id)?.label ?? `Source ${index + 1}`}</button>
    )}</details>}
    {sourceError && <p role="alert">{sourceError}</p>}
    {source && hasCitationPreview(source) && <div className="ask-source-preview"><strong>{source.label}</strong><button type="button" aria-label="Close source preview" onClick={() => setSource(null)}>×</button>{source.message && <p>{source.message}</p>}{source.target?.kind === 'web' && <><p>{source.excerpt}</p>
      <p>{source.target.fetched ? 'Page read' : 'Search snippet'} · {new Date(source.target.retrieved_at).toLocaleString()}</p>
      <p><a href={source.target.url} target="_blank" rel="noopener noreferrer">{source.target.url}</a></p>
      <button type="button" className="btn" onClick={() => { if (source.target?.kind === 'web') onInspectProposal('Save web source', { action_id: 'import.urls', scope: { kind: 'project' }, params: { urls: [source.target.url] }, sheet_name: 'Web sources', output_names: {} }); }}>Save to project</button>
    </>}</div>}
    {event.kind === 'answer' && <div className="ask-answer-actions"><button type="button" aria-label="Copy answer text" title="Copy answer text" onClick={() => void copy(false)}><Copy size={14} aria-hidden /></button><button type="button" aria-label="Copy answer with sources" title="Copy answer with sources" onClick={() => void copy(true)}><Files size={14} aria-hidden /></button>{copyStatus && <span role="status">{copyStatus}</span>}</div>}
  </div>;
}
