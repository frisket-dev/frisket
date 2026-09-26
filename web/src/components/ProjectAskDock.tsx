import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, Plus, Send, Square } from 'lucide-react';
import type { GeneratedActionDraft, SheetMeta } from '../api/types';
import type { AskScope, AskCitation } from '../api/projectQA';
import type { SelectorChoice } from '../api/selectorChoices';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { usePoll } from '../hooks/usePoll';
import { SelectorField } from '../engine-selector/SelectorField';
import { PanelEmpty } from './PanelPrimitives';
import { AskEventContent } from './project-ask/AskEventContent';
import { AskThreadControls } from './project-ask/AskThreadControls';
import { AskSourcePicker } from './project-ask/AskSourcePicker';
import './ProjectAskDock.css';


export function ProjectAskDock({ initialScope, sheets, onClose, onInspectProposal, onOpenSource }: {
  onOpenSource(citation: AskCitation): void;
  initialScope: AskScope; sheets: SheetMeta[]; onClose(): void; onInspectProposal(title: string, spec: GeneratedActionDraft): void;
}) {
  const { qa, chromePreferences: { projectId } } = useWorkspaceStores();
  const state = useSelector(qa.store, (s) => s);
  const [sourcesOpen, setSourcesOpen] = useState(false);
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
  const scopeLabel = (source: NonNullable<AskScope['sources']>[number]) => {
    const name = sheets.find((sheet) => Number(sheet.id) === source.sheet_id)?.name ?? 'Missing sheet';
    return source.kind === 'rows' ? `${name} · ${source.row_ids.length} rows` : source.kind === 'file' ? `${name} · file in row ${source.row_id}` : name;
  };

  return <aside className="ask-dock" aria-label="Ask your project" data-testid="ask-dock">
    <header className="ask-header"><strong>Ask</strong>
      <button type="button" onClick={() => qa.newThread(initialScope)}><Plus size={14} />New conversation</button>
      <button type="button" aria-label="Collapse Ask" onClick={onClose}><ChevronLeft size={18} /></button>
    </header>
    <div className="ask-thread-picker"><select aria-label="Conversation" value={state.thread?.id ?? ''} onChange={(event) => { if (event.target.value) void qa.open(event.target.value); }}>
      <option value="">New conversation</option>
      {state.threads.map((thread) => <option key={thread.id} value={thread.id}>{thread.title}</option>)}
    </select>{state.thread && <AskThreadControls key={state.thread.id} thread={state.thread} active={!!state.activeTurn} />}</div>
    {state.hasMoreThreads && <button type="button" onClick={() => void qa.loadMoreThreads()}>More conversations</button>}
    <div className="ask-scope">
      <span>Sources</span><button type="button" className="btn" onClick={() => setSourcesOpen(true)}>Add sources</button>
      <div className="ask-scope-chips">{state.scope?.kind === 'project' ? <span>Whole project</span> : state.scope?.sources?.map((source, index) => <span key={JSON.stringify(source)}>{scopeLabel(source)}
        <button type="button" aria-label={`Remove ${scopeLabel(source)}`} onClick={() => { const sources = state.scope?.sources?.filter((_, i) => i !== index); qa.setOptions({ scope: sources?.length ? { kind: 'sources', sources } : null }); }}>×</button>
      </span>)}</div>
      {!state.scope && <p>Choose sources before asking a question.</p>}
    </div>
    {sourcesOpen && <AskSourcePicker scope={state.scope} sheets={sheets} onClose={() => setSourcesOpen(false)} onApply={(scope) => { qa.setOptions({ scope }); setSourcesOpen(false); }} />}
    <div className="ask-history" ref={historyRef} aria-live="polite" aria-relevant="additions">
      {state.hasEarlier && <button type="button" disabled={state.busy} onClick={() => void qa.loadEarlier()}>Load earlier messages</button>}
      {!state.events.length && <PanelEmpty>Ask a question about your sources, or explore what they contain.</PanelEmpty>}
      {state.events.map((event) => <AskEventContent key={`${event.thread_id}:${event.seq}`} event={event} onInspectProposal={onInspectProposal} onOpenSource={onOpenSource} />)}
      {state.activeTurn && <div className="ask-status" role="status">{state.activeTurn.status === 'stopping' ? 'Stopping…' : 'Investigating…'}</div>}
    </div>
    <div className="ask-composer">
      {state.error && <p className="ask-error" role="alert">{state.error}</p>}
      {tooManyRows && <p className="ask-error">Choose up to 1,000 rows, or change the scope to the whole sheet.</p>}
      <SelectorField projectId={projectId} label="Model" recentNamespace={`${projectId}:ask`}
        query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'project_ask', ...(state.model ? { model: state.model } : {}) } }}
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
