import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronLeft, Plus, Send, Square } from 'lucide-react';
import type { ActionCatalogEntry, GeneratedActionDraft, SheetMeta } from '../api/types';
import type { AskScope, AskCitation } from '../api/projectQA';
import type { SelectorChoice } from '../api/selectorChoices';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { usePoll } from '../hooks/usePoll';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { SelectorField } from '../engine-selector/SelectorField';
import { MenuPop } from './MenuPop';
import { PanelEmpty } from './PanelPrimitives';
import { AskEventContent } from './project-ask/AskEventContent';
import { askActionProposal } from './project-ask/actionProposal';
import { AskThreadControls } from './project-ask/AskThreadControls';
import { AskSourcePicker } from './project-ask/AskSourcePicker';
import { compactAskToolEvents } from './project-ask/activityEvents';
import { AskWorkingActivity } from './project-ask/AskToolActivity';
import './ProjectAskDock.css';

export function ProjectAskDock({ initialScope, sheets, onClose, onInspectProposal, onOpenSource, onOpenAction }: {
  onOpenSource(citation: AskCitation): void;
  initialScope: AskScope; sheets: SheetMeta[]; onClose(): void; onInspectProposal(title: string, spec: GeneratedActionDraft): void;
  onOpenAction?(actionId: string): void;
}) {
  const { qa, chromePreferences: { projectId } } = useWorkspaceStores();
  const actionCatalog = useActionCatalogHandle();
  const state = useSelector(qa.store, (s) => s);
  const availableActions = useSelector(actionCatalog.store, (snapshot): readonly ActionCatalogEntry[] => (
    snapshot.status === 'ready' ? snapshot.catalog?.actions ?? [] : []
  ));
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [threadMenuOpen, setThreadMenuOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [choice, setChoice] = useState<SelectorChoice | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const threadMenuRef = useRef<HTMLDivElement>(null);
  const threadTriggerRef = useRef<HTMLButtonElement>(null);
  const optionsMenuRef = useRef<HTMLDivElement | null>(null);
  const optionsTriggerRef = useRef<HTMLButtonElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const scrollNextMessage = useRef(false);
  const setOptionsMenuRef = useCallback((node: HTMLDivElement | null) => {
    optionsMenuRef.current = node;
    if (node && node.getAttribute('popover') !== 'manual') node.setAttribute('popover', 'manual');
  }, []);
  useEffect(() => { qa.syncContextScope(initialScope); void qa.initialize(initialScope); }, [qa, initialScope]);
  usePoll(qa.refresh, { active: !!state.activeTurn, intervalMs: 1000, guardOverlap: true });
  useEffect(() => {
    const node = historyRef.current;
    if (!node) return;
    if (scrollNextMessage.current || node.scrollHeight - node.scrollTop - node.clientHeight < 240) {
      node.scrollTop = node.scrollHeight;
      scrollNextMessage.current = false;
    }
  }, [state.events.length]);
  useNativePopover(threadMenuRef, () => setThreadMenuOpen(false), {
    enabled: threadMenuOpen,
    ignoreSelector: '[data-testid="ask-thread-menu-trigger"]',
    focusRestore: true,
  });
  useNativePopover(optionsMenuRef, () => setOptionsOpen(false), {
    enabled: optionsOpen,
    ignoreSelector: '[data-testid="ask-options-trigger"]',
    focusRestore: true,
  });
  const threadMenuPosition = useAnchoredPosition(threadTriggerRef, { enabled: threadMenuOpen, align: 'left', width: 260, gap: 4 });
  const optionsMenuPosition = useAnchoredPosition(optionsTriggerRef, { enabled: optionsOpen, align: 'left', width: 300, gap: 6 });
  const requestModel = state.model ?? (choice?.authored_selection.kind === 'model' ? choice.authored_selection.model : null);
  const tooManyRows = (state.scope?.sources ?? []).reduce((count, source) => count + (source.kind === 'rows' ? source.row_ids.length : 0), 0) > 1000;
  const canSend = !!requestModel && choice?.can_run === true && choice.authored_selection.kind === 'model'
    && choice.authored_selection.model === requestModel && !state.busy && !!state.draft.trim() && !!state.scope && !tooManyRows;
  const scopeLabel = (source: NonNullable<AskScope['sources']>[number]) => {
    const name = sheets.find((sheet) => Number(sheet.id) === source.sheet_id)?.name ?? 'Missing sheet';
    return source.kind === 'rows' ? `${name} · ${source.row_ids.length} rows` : source.kind === 'file' ? `${name} · file in row ${source.row_id}` : name;
  };
  const contextIsSelection = initialScope.kind === 'sources' && (initialScope.sources ?? []).some((source) => source.kind === 'rows');
  const scopeMatchesCurrentView = state.scope?.kind === 'project' || JSON.stringify(state.scope) === JSON.stringify(initialScope);
  const currentViewLabel = contextIsSelection ? 'current selection' : initialScope.kind === 'project' ? 'Entire project' : (initialScope.sources ?? []).map(scopeLabel).join(', ');
  const activeSources = state.scope?.kind === 'sources' ? state.scope.sources ?? [] : [];
  const visibleEvents = useMemo(() => compactAskToolEvents(state.events), [state.events]);
  const actionProposals = useMemo(() => visibleEvents.flatMap((event) => {
    const proposal = askActionProposal(event);
    return proposal ? [proposal] : [];
  }), [visibleEvents]);
  const toolEventsByTurn = useMemo(() => visibleEvents.reduce((byTurn, event) => {
    if (event.kind === 'tool_started' || event.kind === 'tool_completed') {
      byTurn.set(event.turn_id, [...(byTurn.get(event.turn_id) ?? []), event]);
    }
    return byTurn;
  }, new Map<string, typeof visibleEvents>()), [visibleEvents]);
  const referencedProposalSeqs = useMemo(() => {
    const referenced = new Set<number>();
    for (const event of visibleEvents) {
      if (event.kind !== 'answer' && event.kind !== 'assistant') continue;
      const text = typeof event.payload.text === 'string' ? event.payload.text : '';
      for (const match of text.matchAll(/\[[^\]]+\]\(#action-([1-9]\d*)\)/g)) {
        const seq = Number(match[1]);
        if (actionProposals.some((proposal) => proposal.event.turn_id === event.turn_id && proposal.event.seq === seq)) referenced.add(seq);
      }
    }
    return referenced;
  }, [actionProposals, visibleEvents]);
  const send = () => {
    scrollNextMessage.current = true;
    qa.setOptions({ model: requestModel });
    void qa.send();
  };
  const resizeComposer = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
    textarea.style.overflowY = textarea.scrollHeight > 180 ? 'auto' : 'hidden';
  }, []);
  useLayoutEffect(resizeComposer, [resizeComposer, state.draft]);
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return undefined;
    let width = textarea.clientWidth;
    const observer = new ResizeObserver(() => {
      if (textarea.clientWidth === width) return;
      width = textarea.clientWidth;
      resizeComposer();
    });
    observer.observe(textarea);
    return () => observer.disconnect();
  }, [resizeComposer]);

  return <aside className="ask-dock" aria-label="Ask your project" data-testid="ask-dock">
    <header className="ask-header"><strong>Ask</strong>
      <button type="button" className="ask-thread-trigger" ref={threadTriggerRef} data-testid="ask-thread-menu-trigger" aria-haspopup="menu" aria-expanded={threadMenuOpen} onClick={() => setThreadMenuOpen((open) => !open)}>
        <span>{state.thread?.title ?? 'New conversation'}</span><ChevronDown size={14} aria-hidden />
      </button>
      {threadMenuOpen && <MenuPop ref={threadMenuRef} className="ask-thread-menu" style={threadMenuPosition ? { top: threadMenuPosition.top, bottom: threadMenuPosition.bottom, left: threadMenuPosition.left, width: threadMenuPosition.width } : { visibility: 'hidden' }}>
        {state.threads.length === 0 && <p className="menu-empty">No saved conversations.</p>}
        {state.threads.map((thread) => <button key={thread.id} type="button" role="menuitemradio" aria-checked={thread.id === state.thread?.id} className={`menu-item${thread.id === state.thread?.id ? ' current' : ''}`} onClick={() => { setThreadMenuOpen(false); void qa.open(thread.id); }}><span className="menu-item-name">{thread.title}</span></button>)}
        {state.hasMoreThreads && <button type="button" role="menuitem" className="menu-item" onClick={() => void qa.loadMoreThreads()}>Load more conversations</button>}
      </MenuPop>}
      <button type="button" className="ask-new-thread" onClick={() => qa.newThread(initialScope)}><Plus size={14} aria-hidden />New</button>
      {state.thread && <AskThreadControls key={state.thread.id} thread={state.thread} active={!!state.activeTurn} />}
      <button type="button" aria-label="Collapse Ask" onClick={onClose}><ChevronLeft size={18} /></button>
    </header>
    {sourcesOpen && <AskSourcePicker scope={state.scope} sheets={sheets} onClose={() => setSourcesOpen(false)} onApply={(scope) => { qa.setOptions({ scope }); setSourcesOpen(false); }} />}
    <div className="ask-history" ref={historyRef} aria-live="polite" aria-relevant="additions">
      {state.hasEarlier && <button type="button" disabled={state.busy} onClick={() => void qa.loadEarlier()}>Load earlier messages</button>}
      {!state.events.length && <PanelEmpty>Ask a question about your sources, or explore what they contain.</PanelEmpty>}
      {visibleEvents.map((event) => {
        if (event.kind === 'tool_started' || event.kind === 'tool_completed') {
          const turnEvents = toolEventsByTurn.get(event.turn_id) ?? [];
          if (turnEvents[0] !== event) return null;
          return <AskWorkingActivity key={`working:${event.thread_id}:${event.turn_id}`} events={turnEvents} active={state.activeTurn?.id === event.turn_id} />;
        }
        if (event.kind === 'action_proposal' && referencedProposalSeqs.has(event.seq)) return null;
        return <AskEventContent key={`${event.thread_id}:${event.seq}`} event={event} actionProposals={actionProposals} actionCatalog={availableActions} onOpenAction={onOpenAction} onInspectProposal={onInspectProposal} onOpenSource={onOpenSource} />;
      })}
      {state.activeTurn && !toolEventsByTurn.has(state.activeTurn.id) && <AskWorkingActivity events={[]} active />}
    </div>
    <div className="ask-composer">
      {state.error && <p className="ask-error" role="alert">{state.error}</p>}
      {tooManyRows && <p className="ask-error">Choose up to 1,000 rows, or change the scope to the whole sheet.</p>}
      <div className="ask-composer-sources" aria-label="Question sources">
        <div className="ask-scope-chips">{activeSources.length ? activeSources.map((source, index) => <span key={JSON.stringify(source)}>{scopeLabel(source)}
          <button type="button" aria-label={`Remove ${scopeLabel(source)}`} onClick={() => { const sources = state.scope?.sources?.filter((_, i) => i !== index) ?? []; qa.setOptions({ scope: sources.length ? { kind: 'sources', sources } : { kind: 'project' } }); }}>×</button>
        </span>) : <span>Entire project</span>}</div>
        <button type="button" className="ask-source-add" aria-label="Add sources" title="Add sources" onClick={() => setSourcesOpen(true)}><Plus size={15} aria-hidden /></button>
      </div>
      {state.scope && !scopeMatchesCurrentView && <div className="ask-scope-mismatch"><span>Viewing {currentViewLabel}</span><button type="button" onClick={() => qa.setOptions({ scope: initialScope })}>{contextIsSelection ? 'Use current selection' : 'Use this sheet'}</button></div>}
      <div className="ask-compose-box">
        <textarea ref={textareaRef} rows={1} aria-label="Question" placeholder={state.thread ? 'Follow up in this conversation…' : 'Ask about your project…'} value={state.draft} onChange={(event) => qa.setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !state.activeTurn && canSend) { event.preventDefault(); send(); } }} />
        <div className="ask-compose-controls">
          <button type="button" className="ask-options-trigger" ref={optionsTriggerRef} data-testid="ask-options-trigger" aria-label="Ask options" title="Ask options" aria-haspopup="dialog" aria-expanded={optionsOpen} onClick={() => setOptionsOpen((open) => !open)}><Plus size={16} aria-hidden /></button>
          <MenuPop ref={setOptionsMenuRef} role="dialog" aria-label="Ask settings" className="ask-options-menu" style={optionsMenuPosition ? { top: optionsMenuPosition.top, bottom: optionsMenuPosition.bottom, left: optionsMenuPosition.left, width: optionsMenuPosition.width } : { visibility: 'hidden' }}>
            <label className="ask-option-check"><input type="checkbox" checked={state.web} onChange={(event) => qa.setOptions({ web: event.target.checked })} />Web</label>
            <label className="ask-option-check"><input type="checkbox" checked={state.suggestActions} onChange={(event) => qa.setOptions({ suggestActions: event.target.checked })} />Suggest actions</label>
            <div className="ask-option-model"><SelectorField projectId={projectId} label="Model" recentNamespace={`${projectId}:ask`}
              query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'project_ask', ...(state.model ? { model: state.model } : {}) } }}
              onCurrentChoiceChange={setChoice}
              onSelect={(selected) => { if (selected.authored_selection.kind === 'model') qa.setOptions({ model: selected.authored_selection.model }); }} /></div>
          </MenuPop>
          {state.activeTurn ? <button type="button" disabled={state.activeTurn.status === 'stopping'} onClick={() => void qa.stop()}><Square size={14} />Stop</button>
            : <button type="button" disabled={!canSend} onClick={send}><Send size={14} />Send</button>}
        </div>
      </div>
    </div>
  </aside>;
}
