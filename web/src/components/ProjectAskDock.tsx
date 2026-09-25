import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, Plus, Send, Square } from 'lucide-react';
import type { SheetMeta } from '../api/types';
import type { AskCitation, AskEvent, AskScope } from '../api/projectQA';
import type { SelectorChoice } from '../api/selectorChoices';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { usePoll } from '../hooks/usePoll';
import { MarkdownView } from '../markdown';
import { SelectorField } from '../engine-selector/SelectorField';
import { PanelEmpty } from './PanelPrimitives';
import './ProjectAskDock.css';

function EventContent({ event }: { event: AskEvent }) {
  const payload = event.payload;
  const { qa } = useWorkspaceStores();
  const [source, setSource] = useState<AskCitation | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  if (event.kind === 'question') return <div className="ask-question">{String(payload.question ?? '')}</div>;
  if (event.kind === 'answer' || event.kind === 'assistant') return <div className="ask-answer">
    <MarkdownView source={String(payload.text ?? '')} />
    {Array.isArray(payload.citation_ids) && <div className="ask-citations">{payload.citation_ids.filter((id): id is string => typeof id === 'string').map((id, index) =>
      <button type="button" key={id} onClick={() => { setSourceError(null); void qa.citation(event.thread_id, id).then(setSource).catch(() => setSourceError('Could not open this source. Please try again.')); }}>Source {index + 1}</button>
    )}</div>}
    {sourceError && <p role="alert">{sourceError}</p>}
    {source && <div className="ask-source-preview"><strong>{source.label}</strong><button type="button" aria-label="Close source preview" onClick={() => setSource(null)}>×</button>{source.message && <p>{source.message}</p>}<p>{source.excerpt}</p></div>}
  </div>;
  if (event.kind === 'tool_started' || event.kind === 'tool_completed') return (
    <details className="ask-tool"><summary>{String(payload.summary ?? payload.tool ?? 'Reading sources')}</summary>
      <p>{String(payload.detail ?? payload.summary ?? '')}</p>
    </details>
  );
  if (event.kind === 'status' && ['stopped', 'interrupted', 'failed'].includes(String(payload.status))) return (
    <p className="ask-status">{payload.status === 'stopped' ? 'Stopped. The work above is saved.' : payload.status === 'interrupted' ? 'Interrupted. Send a follow-up to continue.' : String(payload.error_summary ?? 'This question could not be completed.')}</p>
  );
  return null;
}

export function ProjectAskDock({ initialScope, sheets, onClose }: {
  initialScope: AskScope; sheets: SheetMeta[]; onClose(): void;
}) {
  const { qa, chromePreferences: { projectId } } = useWorkspaceStores();
  const state = useSelector(qa.store, (s) => s);
  const [choice, setChoice] = useState<SelectorChoice | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  useEffect(() => { void qa.initialize(initialScope); }, [qa, initialScope]);
  usePoll(qa.refresh, { active: !!state.activeTurn, intervalMs: 1000, guardOverlap: true });
  useEffect(() => {
    const node = historyRef.current;
    if (node && node.scrollHeight - node.scrollTop - node.clientHeight < 240) node.scrollTop = node.scrollHeight;
  }, [state.events.length]);
  const requestModel = state.model ?? (choice?.authored_selection.kind === 'model' ? choice.authored_selection.model : null);
  const tooManyRows = (state.scope?.sources ?? []).reduce((count, source) => count + (source.kind === 'rows' ? source.row_ids.length : 0), 0) > 1000;
  const canSend = !!requestModel && choice?.can_run === true && choice.authored_selection.kind === 'model'
    && choice.authored_selection.model === requestModel && !state.busy && !!state.draft.trim() && !!state.scope && !tooManyRows;
  const scopeSheet = state.scope?.kind === 'sources' && state.scope.sources?.length === 1 && state.scope.sources[0].kind === 'sheet'
    ? String(state.scope.sources[0].sheet_id) : '';

  return <aside className="ask-dock" aria-label="Ask your project" data-testid="ask-dock">
    <header className="ask-header"><strong>Ask</strong>
      <button type="button" onClick={() => qa.newThread(initialScope)}><Plus size={14} />New conversation</button>
      <button type="button" aria-label="Collapse Ask" onClick={onClose}><ChevronLeft size={18} /></button>
    </header>
    <div className="ask-thread-picker"><select aria-label="Conversation" value={state.thread?.id ?? ''} onChange={(event) => { if (event.target.value) void qa.open(event.target.value); }}>
      <option value="">New conversation</option>
      {state.threads.map((thread) => <option key={thread.id} value={thread.id}>{thread.title}</option>)}
    </select></div>
    <div className="ask-scope"><label>Scope <select aria-label="Question scope" value={state.scope?.kind === 'project' ? 'project' : scopeSheet} onChange={(event) => qa.setOptions({ scope: event.target.value === 'project' ? { kind: 'project' } : { kind: 'sources', sources: [{ kind: 'sheet', sheet_id: Number(event.target.value) }] } })}>
      {!scopeSheet && state.scope?.kind !== 'project' && <option value="">Selected sources</option>}
      {sheets.map((sheet) => <option key={sheet.id} value={sheet.id}>{sheet.name}</option>)}
      <option value="project">Whole project</option>
    </select></label></div>
    <div className="ask-history" ref={historyRef} aria-live="polite" aria-relevant="additions">
      {state.hasEarlier && <button type="button" disabled={state.busy} onClick={() => void qa.loadEarlier()}>Load earlier messages</button>}
      {!state.events.length && <PanelEmpty>Ask a question about your sources, or explore what they contain.</PanelEmpty>}
      {state.events.map((event) => <EventContent key={event.seq} event={event} />)}
      {state.activeTurn && <div className="ask-status" role="status">{state.activeTurn.status === 'stopping' ? 'Stopping…' : 'Investigating…'}</div>}
    </div>
    <div className="ask-composer">
      {state.error && <p className="ask-error" role="alert">{state.error}</p>}
      {tooManyRows && <p className="ask-error">Choose up to 1,000 rows, or change the scope to the whole sheet.</p>}
      <SelectorField projectId={projectId} label="Model" recentNamespace={`${projectId}:ask`}
        query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'copilot', ...(state.model ? { model: state.model } : {}) } }}
        onCurrentChoiceChange={setChoice}
        onSelect={(selected) => { if (selected.authored_selection.kind === 'model') qa.setOptions({ model: selected.authored_selection.model }); }} />
      <div className="ask-compose-box">
        <textarea aria-label="Question" placeholder={state.thread ? 'Follow up in this conversation…' : 'Ask about your project…'} value={state.draft} onChange={(event) => qa.setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !state.activeTurn && canSend) { event.preventDefault(); qa.setOptions({ model: requestModel }); void qa.send(); } }} />
        <div className="ask-compose-controls">
          <label><input type="checkbox" checked={state.web} onChange={(event) => qa.setOptions({ web: event.target.checked })} />Web</label>
          <label><input type="checkbox" checked={state.suggestActions} onChange={(event) => qa.setOptions({ suggestActions: event.target.checked })} />Suggest actions</label>
          {state.activeTurn ? <button type="button" disabled={state.activeTurn.status === 'stopping'} onClick={() => void qa.stop()}><Square size={14} />Stop</button>
            : <button type="button" disabled={!canSend} onClick={() => { qa.setOptions({ model: requestModel }); void qa.send(); }}><Send size={14} />Send</button>}
        </div>
      </div>
    </div>
  </aside>;
}
