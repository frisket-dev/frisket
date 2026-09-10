import { useEffect, useReducer, useRef, useState } from 'react';
import { Bot, Send, Play, X, Upload, ChevronDown, ChevronUp } from 'lucide-react';
import { listProviders, type CopilotProposal } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { defaultCopilotModel } from '../actions/model';
import { MarkdownView } from '../markdown';
import { ModelPicker } from './ModelPicker';

// Copilot is a floating chat UI summoned by the chrome ✧ button — its
// open/closed state is
// owned by the caller (chrome state), so this component always renders its chat
// content when mounted and delegates × to `onClose`. Messages go to POST
// /copilot; each returned action proposal renders as a card with a Run button
// that delegates its hidden spec to the action run adapter, and an Inspect
// button that opens the configure-first drawer. Contract testids (see
// copilot.spec.ts): copilot-panel / copilot-input / copilot-send /
// copilot-proposal / copilot-run / copilot-inspect.

// User-level preference (not per project): the last model the copilot chatted
// with. Absent → derive a default from the live provider catalog, mirroring
// the server's own preference order (defaultCopilotModel).
const COPILOT_MODEL_STORAGE_KEY = 'frisket:copilot-model';

function useCopilotModel(): [string | null, (modelId: string) => void] {
  const [model, setModel] = useState<string | null>(() => {
    try {
      return localStorage.getItem(COPILOT_MODEL_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  useEffect(() => {
    if (model) return undefined;
    let cancelled = false;
    listProviders()
      .then((catalog) => {
        if (!cancelled) setModel((prev) => prev ?? defaultCopilotModel(catalog));
      })
      .catch(() => {
        if (!cancelled) setModel((prev) => prev ?? defaultCopilotModel(null));
      });
    return () => {
      cancelled = true;
    };
  }, [model]);
  const select = (modelId: string) => {
    setModel(modelId);
    try {
      localStorage.setItem(COPILOT_MODEL_STORAGE_KEY, modelId);
    } catch {
      // private mode etc. — the in-memory selection still applies
    }
  };
  return [model, select];
}

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  proposals?: CopilotProposal[];
  /** The model decided the project needs data imported before it can act. */
  needsImport?: boolean;
}

interface CopilotState {
  messages: ChatMessage[];
  draft: string;
  busy: boolean;
  error: string | null;
  ran: Set<string>;
}

type CopilotAction =
  | { type: 'draftChanged'; value: string }
  | { type: 'sendStarted'; history: ChatMessage[] }
  | {
      type: 'sendSucceeded';
      history: ChatMessage[];
      assistantMessage: ChatMessage;
    }
  | { type: 'requestFailed'; error: string }
  | { type: 'runStarted' }
  | { type: 'proposalQueued'; key: string }
  | { type: 'runFailed'; error: string };

function createInitialCopilotState(): CopilotState {
  return {
    messages: [],
    draft: '',
    busy: false,
    error: null,
    ran: new Set(),
  };
}

function copilotReducer(
  state: CopilotState,
  action: CopilotAction,
): CopilotState {
  switch (action.type) {
    case 'draftChanged':
      return { ...state, draft: action.value };
    case 'sendStarted':
      return {
        ...state,
        messages: action.history,
        draft: '',
        busy: true,
        error: null,
      };
    case 'sendSucceeded':
      return {
        ...state,
        messages: [...action.history, action.assistantMessage],
        busy: false,
      };
    case 'requestFailed':
      return { ...state, busy: false, error: action.error };
    case 'runStarted':
      return { ...state, error: null };
    case 'proposalQueued':
      return { ...state, ran: new Set(state.ran).add(action.key) };
    case 'runFailed':
      return { ...state, error: action.error };
  }
}

export function CopilotPanel({
  onRunProposal,
  onInspectProposal,
  onImportNeeded,
  onClose,
  collapsed = false,
  onCollapsedChange,
}: {
  onRunProposal(proposal: CopilotProposal): Promise<boolean>;
  onInspectProposal(proposal: CopilotProposal): void;
  /** The needs-import CTA: close Copilot and open the single Import workspace. */
  onImportNeeded?(): void;
  /** Dismiss the popover (chrome ✧ / the header ×). */
  onClose?(): void;
  /**
   * Collapsed = header-only strip: thread + compose are
   * hidden; the chevron next to × re-expands. Controlled by the popover host
   * (per-mount state, never persisted) so it can also keep the collapsed strip
   * from light-dismissing. Inspecting a proposal collapses instead of closing.
   */
  collapsed?: boolean;
  onCollapsedChange?(collapsed: boolean): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const [state, dispatch] = useReducer(
    copilotReducer,
    undefined,
    createInitialCopilotState,
  );
  const { messages, draft, busy, error, ran } = state;
  const nextMessageId = useRef(1);
  const [model, selectModel] = useCopilotModel();

  async function send() {
    const text = draft.trim();
    // !model: sending before the default resolves would fall back to the
    // server default, which can differ from the provider selected by the
    // live catalog.
    if (!text || busy || !model) return;
    const userMessage: ChatMessage = {
      id: `user-${nextMessageId.current++}`,
      role: 'user',
      content: text,
    };
    const history: ChatMessage[] = [...messages, userMessage];
    dispatch({ type: 'sendStarted', history });
    try {
      const res = await projectApi.copilotChat(
        history.map((m) => ({ role: m.role, content: m.content })),
        model,
      );
      dispatch({
        type: 'sendSucceeded',
        history,
        assistantMessage: {
          id: `assistant-${nextMessageId.current++}`,
          role: 'assistant',
          content: res.reply,
          // A needs-import reply never carries an invented proposal — the CTA,
          // not an action card, is its only actionable surface.
          proposals: res.needsImport ? [] : res.proposals,
          needsImport: res.needsImport,
        },
      });
    } catch (e) {
      dispatch({
        type: 'requestFailed',
        error: e instanceof Error ? e.message : 'copilot request failed',
      });
    }
  }

  async function run(p: CopilotProposal, key: string) {
    dispatch({ type: 'runStarted' });
    try {
      const queued = await onRunProposal(p);
      if (queued) dispatch({ type: 'proposalQueued', key });
    } catch (e) {
      dispatch({
        type: 'runFailed',
        error: e instanceof Error ? e.message : 'run failed',
      });
    }
  }

  return (
    <aside
      className="copilot-panel"
      data-testid="copilot-panel"
      data-collapsed={collapsed ? 'true' : undefined}
    >
      <header className="copilot-head">
        <Bot size={16} />
        <span>Copilot</span>
        <span className="copilot-head-actions">
          {onCollapsedChange && (
            <button
              type="button"
              className="icon-btn"
              data-testid="copilot-collapse"
              onClick={() => onCollapsedChange(!collapsed)}
              aria-label={collapsed ? 'Expand copilot' : 'Collapse copilot'}
              aria-expanded={!collapsed}
            >
              {collapsed ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
            </button>
          )}
          <button
            type="button"
            className="icon-btn"
            onClick={() => onClose?.()}
            aria-label="Close copilot"
          >
            <X size={16} />
          </button>
        </span>
      </header>

      {collapsed ? null : (
      <div className="copilot-thread">
        {messages.length === 0 && (
          <p className="copilot-hint">
            Ask for a column or a transformation — e.g. “classify each story by
            news beat”. I’ll propose a runnable action.
          </p>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`copilot-msg copilot-${m.role}`}>
            {m.role === 'assistant' ? (
              <MarkdownView
                className="copilot-bubble copilot-markdown"
                source={m.content}
                testId="copilot-response-markdown"
                decodeEscapes
              />
            ) : (
              <div className="copilot-bubble">{m.content}</div>
            )}
            {m.needsImport && (
              <button
                type="button"
                className="btn btn-primary copilot-import-cta"
                data-testid="copilot-import-cta"
                onClick={() => onImportNeeded?.()}
              >
                <Upload size={13} />
                Import data
              </button>
            )}
            {m.proposals?.map((p) => {
              const key = proposalKey(m.id, p);
              const actionId = p.spec.action_id;
              return (
                <div
                  key={key}
                  className="copilot-proposal"
                  data-testid="copilot-proposal"
                >
                  {actionId !== '' && (
                    <span
                      className="copilot-proposal-kind"
                      data-testid="copilot-proposal-kind"
                    >
                      {humanizeActionKind(actionId)}
                    </span>
                  )}
                  <span className="copilot-proposal-title">{p.title}</span>
                  <button
                    type="button"
                    className="btn copilot-inspect"
                    data-testid="copilot-inspect"
                    onClick={() => {
                      // Inspect COLLAPSES the popover instead of closing it:
                      // the thread survives next to the
                      // configure-first drawer the proposal opens into.
                      onCollapsedChange?.(true);
                      onInspectProposal(p);
                    }}
                  >
                    Inspect
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary copilot-run"
                    data-testid="copilot-run"
                    disabled={ran.has(key)}
                    onClick={() => run(p, key)}
                  >
                    <Play size={13} />
                    {ran.has(key) ? 'Running…' : 'Run'}
                  </button>
                </div>
              );
            })}
          </div>
        ))}
        {busy && <div className="copilot-msg copilot-assistant">…</div>}
        {error && <div className="copilot-error">{error}</div>}
      </div>
      )}

      {collapsed ? null : (
      <>
      <div className="copilot-model-row" data-testid="copilot-model-row">
        <span className="copilot-model-label" id="copilot-model-label">
          Model
        </span>
        {model && (
          <ModelPicker
            value={model}
            onChange={selectModel}
            ariaLabelledBy="copilot-model-label"
          />
        )}
      </div>
      <div className="copilot-compose">
        <textarea
          aria-label="Ask the copilot"
          data-testid="copilot-input"
          value={draft}
          onChange={(e) =>
            dispatch({ type: 'draftChanged', value: e.target.value })
          }
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="Ask the copilot…"
          rows={2}
        />
        <button
          type="button"
          className="btn-primary copilot-send-button"
          data-testid="copilot-send"
          onClick={() => void send()}
          disabled={busy || !draft.trim() || !model}
          aria-label="Send"
        >
          <Send size={15} />
        </button>
      </div>
      </>
      )}
    </aside>
  );
}

function proposalKey(messageId: string, proposal: CopilotProposal) {
  return `${messageId}:${proposal.kind}:${proposal.title}:${JSON.stringify(proposal.spec)}`;
}

/** 'map.regex_extract' → 'Regex extract': strip the family prefix,
 * replace underscores, sentence-case. */
function humanizeActionKind(actionKind: string): string {
  const tail = actionKind.slice(actionKind.indexOf('.') + 1) || actionKind;
  const words = tail.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
