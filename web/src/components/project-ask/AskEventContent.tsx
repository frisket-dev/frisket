import { useState } from 'react';
import type { AskCitation, AskEvent } from '../../api/projectQA';
import { isCopilotRegisteredActionDraft, type CopilotRegisteredActionDraft } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { MarkdownView } from '../../markdown';

export function AskEventContent({ event, onInspectProposal, onOpenSource }: { event: AskEvent; onOpenSource(citation: AskCitation): void; onInspectProposal(title: string, spec: CopilotRegisteredActionDraft): void }) {
  const payload = event.payload;
  const { qa } = useWorkspaceStores();
  const [source, setSource] = useState<AskCitation | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const openSource = (id: string) => {
    setSourceError(null);
    void qa.citation(event.thread_id, id).then((citation) => { setSource(citation); onOpenSource(citation); }).catch(() => setSourceError('Could not open this source. Please try again.'));
  };
  if (event.kind === 'action_proposal') {
    const proposal = payload.proposal;
    if (!proposal || typeof proposal !== 'object' || Array.isArray(proposal)) return null;
    const spec = proposal.spec;
    if (!spec || typeof spec !== 'object' || Array.isArray(spec) || !isCopilotRegisteredActionDraft(spec)) return null;
    const title = String(proposal.title ?? 'Suggested action');
    return <div className="ask-proposal"><strong>{title}</strong><p>Review the settings before running this action.</p><button type="button" className="btn" onClick={() => onInspectProposal(title, spec)}>Open action</button></div>;
  }
  if (event.kind === 'result_suggestion') return <div className="ask-result"><strong>{String(payload.title ?? 'Matching records')}</strong><p>{typeof payload.total === 'number' ? `${payload.total.toLocaleString()} matching rows` : 'Search results'}</p><button type="button" className="btn" onClick={() => typeof payload.citation_id === 'string' && openSource(payload.citation_id)}>Open results</button>{source?.message && <p>{source.message}</p>}{sourceError && <p role="alert">{sourceError}</p>}</div>;
  if (event.kind === 'question') return <div className="ask-question">{String(payload.question ?? '')}</div>;
  if (event.kind === 'answer' || event.kind === 'assistant') return <div className="ask-answer">
    <MarkdownView source={String(payload.text ?? '')} />
    {Array.isArray(payload.citation_ids) && <div className="ask-citations">{payload.citation_ids.filter((id): id is string => typeof id === 'string').map((id, index) =>
      <button type="button" key={id} onClick={() => openSource(id)}>{event.citations?.find((citation) => citation.id === id)?.label ?? `Source ${index + 1}`}</button>
    )}</div>}
    {sourceError && <p role="alert">{sourceError}</p>}
    {source && <div className="ask-source-preview"><strong>{source.label}</strong><button type="button" aria-label="Close source preview" onClick={() => setSource(null)}>×</button>{source.message && <p>{source.message}</p>}<p>{source.excerpt}</p></div>}
  </div>;
  const toolLabels: Record<string, string> = { inspect_sheets: 'Checking sources', read_rows: 'Reading records', query_rows: 'Checking matching records', search_cells: 'Searching project content', open_source: 'Reading a source', propose_action: 'Preparing an action suggestion', search_web: 'Searching the web', open_web_page: 'Reading a web page' };
  if (event.kind === 'tool_started' || event.kind === 'tool_completed') return (
    <details className="ask-tool"><summary>{String(payload.summary ?? toolLabels[String(payload.tool)] ?? 'Reading sources')}</summary>
      <p>{String(payload.detail ?? payload.summary ?? '')}</p>
    </details>
  );
  if (event.kind === 'status' && ['stopped', 'interrupted', 'failed'].includes(String(payload.status))) return (
    <p className="ask-status">{payload.status === 'stopped' ? 'Stopped. The work above is saved.' : payload.status === 'interrupted' ? 'Interrupted. Send a follow-up to continue.' : String(payload.error_summary ?? 'This question could not be completed.')}</p>
  );
  return null;
}

