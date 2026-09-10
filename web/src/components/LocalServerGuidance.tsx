// Install-guidance empty states for the local LLM server slot: every dead end
// for the 'ollama' catalog entry (any OpenAI-compatible local server, not just
// Ollama proper) becomes
// instructions aimed at THE MACHINE THAT WILL RUN THE MODEL SERVER, never
// "your computer" -- the browser rendering this pane, the frisket host, and
// the model-server host are three potentially-different machines. Platform
// is a selectable tab, never sniffed from navigator (we cannot know which
// machine will actually run the server). Renders in two places: a compact
// pane inside the ModelPicker popover, and the full settings section
// (variant='full' adds the platform-tab install walkthrough).

import { useState } from 'react';
import { Check, Copy, Download, RefreshCcw } from 'lucide-react';
import { ApiError, startArtifactPull } from '../api/open';
import type { LocalHttpEndpointEntry, ModelPullDto } from '../api/types';
import { ModelPullProgress } from './ModelPullProgress';

export type LocalServerGuidanceStateId =
  | 'unreachable'
  | 'unauthorized'
  | 'empty_native'
  | 'empty_compat';

/** Pure classifier over catalog facts --
 *  never guesses: a model list absent/unknown protocol means the catalog
 *  probe has no authoritative listing, so this returns null rather than
 *  claiming "empty". `unauthorized` is checked first because a 401/403 is a
 *  distinct dead end from "not running", not a flavor of unreachable.
 *  Kept in this component file (rather than the repo's usual split-out-non-
 *  component-exports idiom) because LocalServerGuidance.test.tsx imports both
 *  names from one module -- the one non-component export here, so
 *  react-refresh is scoped off for just this block. */
/* eslint-disable-next-line react-refresh/only-export-components */
export function localServerGuidanceState(
  entry: LocalHttpEndpointEntry,
): LocalServerGuidanceStateId | null {
  if (entry.auth_status === 'unauthorized') return 'unauthorized';
  if (entry.reachable === false) return 'unreachable';
  const installed = entry.installed_models;
  if (entry.reachable === true && Array.isArray(installed) && installed.length === 0) {
    if (entry.protocol === 'ollama_native') return 'empty_native';
    if (entry.protocol === 'openai_compatible') return 'empty_compat';
  }
  return null;
}

type Platform = 'macos' | 'linux' | 'windows';

const PLATFORM_LABEL: Record<Platform, string> = {
  macos: 'macOS',
  linux: 'Linux',
  windows: 'Windows',
};

// One command per platform where a single copy-pasteable command exists;
// Windows has no single-line install command worth showing (installer GUI).
const PLATFORM_COMMAND: Record<Platform, string | null> = {
  macos: 'brew install ollama',
  linux: 'curl -fsSL https://ollama.com/install.sh | sh',
  windows: null,
};

// The onboarding model is deliberately tiny: Ollama publishes this tag as a
// 523 MB download, and the opt-in live proof in tests/ai/test_ollama_live.py
// exercises it through Frisket's structured-output repair path.
const PULL_MODEL = 'qwen3:0.6b';
const PULL_COMMAND = `ollama pull ${PULL_MODEL}`;

/** Narrows a 409 pull_busy error's `details.active` (see api/real.ts's
 *  modelPullHttp, which preserves the full detail payload) to a ModelPullDto
 *  before rendering it -- the catalog is UX, not a security boundary, but we
 *  still never trust an error body's shape blindly. */
function isModelPullDto(value: unknown): value is ModelPullDto {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { id?: unknown }).id === 'number' &&
    typeof (value as { status?: unknown }).status === 'string'
  );
}

type DownloadPhase = 'idle' | 'confirm' | 'active' | 'error';

interface LocalServerGuidanceProps {
  entry: LocalHttpEndpointEntry;
  variant: 'compact' | 'full';
  onRecheck: () => void | Promise<void>;
}

export function LocalServerGuidance({ entry, variant, onRecheck }: LocalServerGuidanceProps) {
  // Default tab is macOS -- a fixed, deterministic default, never
  // navigator.platform sniffing (the browser's OS tells us nothing about
  // which machine will run the model server).
  const [platform, setPlatform] = useState<Platform>('macos');
  const [copied, setCopied] = useState(false);
  const [recheckBusy, setRecheckBusy] = useState(false);
  const [recheckError, setRecheckError] = useState<string | null>(null);

  // In-app pull affordance, shown
  // only in the empty_native state when the catalog says pull_enabled. Kept
  // as hooks at top level (not inside the state === 'empty_native' branch
  // below) so the rules of hooks hold regardless of which state renders.
  const [downloadPhase, setDownloadPhase] = useState<DownloadPhase>('idle');
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [downloadPull, setDownloadPull] = useState<ModelPullDto | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const state = localServerGuidanceState(entry);
  if (!state) return null;

  const startDownload = async () => {
    setDownloadBusy(true);
    setDownloadError(null);
    try {
      const result = await startArtifactPull(
        `ollama/@${entry.endpoint_id}/${PULL_MODEL}`,
      );
      setDownloadPull(result.pull);
      setDownloadPhase('active');
    } catch (caught) {
      // pull_busy (409): another pull for this host is already running --
      // show ITS progress instead of a bare error (the catalog re-verifies
      // at execution time, so a stale browser snapshot degrades into
      // precise remediation, not a dead end).
      if (caught instanceof ApiError && caught.code === 'pull_busy' && isModelPullDto(caught.details?.active)) {
        setDownloadPull(caught.details.active);
        setDownloadPhase('active');
      } else {
        setDownloadError(caught instanceof Error ? caught.message : String(caught));
        setDownloadPhase('error');
      }
    } finally {
      setDownloadBusy(false);
    }
  };

  const recheck = (
    <button
      type="button"
      className="btn btn-compact guidance-recheck"
      data-testid="guidance-recheck"
      disabled={recheckBusy}
      aria-busy={recheckBusy}
      onClick={() => {
        setRecheckBusy(true);
        setRecheckError(null);
        void Promise.resolve(onRecheck()).catch((caught) => {
          setRecheckError(caught instanceof Error ? caught.message : String(caught));
        }).finally(() => setRecheckBusy(false));
      }}
    >
      <RefreshCcw size={12} className={recheckBusy ? 'spin' : undefined} />
      {recheckBusy ? 'Rechecking…' : 'Recheck'}
    </button>
  );

  let body: React.ReactNode;

  if (state === 'unreachable') {
    if (variant === 'compact') {
      body = (
        <p className="local-server-guidance-copy">
          No local model server reachable on the machine that will run your
          models. Install one there, then Recheck.
        </p>
      );
    } else {
      const command = PLATFORM_COMMAND[platform];
      body = (
        <>
          <p className="local-server-guidance-copy">
            Install a local model server on the machine that will run your
            models — that may not be the computer you are using right now.
          </p>
          <div className="guidance-tabs" role="tablist" aria-label="Install platform">
            {(Object.keys(PLATFORM_LABEL) as Platform[]).map((p) => (
              <button
                key={p}
                type="button"
                role="tab"
                aria-selected={p === platform}
                className={`guidance-tab ${p === platform ? 'is-active' : ''}`}
                data-testid={`guidance-tab-${p}`}
                onClick={() => setPlatform(p)}
              >
                {PLATFORM_LABEL[p]}
              </button>
            ))}
          </div>
          {command ? (
            <>
              <code className="guidance-command" data-testid="guidance-command">{command}</code>
              {platform === 'macos' && (
                <p className="settings-help">
                  Alternative: download the installer from{' '}
                  <a href="https://ollama.com/download" target="_blank" rel="noreferrer">
                    ollama.com/download
                  </a>.
                </p>
              )}
            </>
          ) : (
            <p className="local-server-guidance-copy">
              Download the installer from{' '}
              <a href="https://ollama.com/download" target="_blank" rel="noreferrer">
                ollama.com/download
              </a>{' '}
              and run it on that machine.
            </p>
          )}
        </>
      );
    }
  } else if (state === 'unauthorized') {
    body = (
      <p className="local-server-guidance-copy">
        The server on the machine that runs your models rejected
        authentication{entry.detail ? `: ${entry.detail}` : '.'} Fix its
        credentials, then Recheck.
      </p>
    );
  } else if (state === 'empty_native') {
    body = (
      <>
        <p className="local-server-guidance-copy">
          The server is running but has no models installed yet. Pull one on
          the machine that runs it:
        </p>
        {entry.pull_enabled === true && downloadPhase === 'confirm' && (
          <div className="guidance-download-confirm" data-testid="guidance-download-confirm">
            <p className="local-server-guidance-copy">
              This downloads about 523 MB to the machine running your local
              server.
            </p>
            <div className="settings-row-actions">
              <button
                type="button"
                className="btn btn-primary"
                data-testid="guidance-download-confirm-yes"
                disabled={downloadBusy}
                onClick={() => void startDownload()}
              >
                Confirm
              </button>
              <button
                type="button"
                className="btn"
                data-testid="guidance-download-cancel"
                disabled={downloadBusy}
                onClick={() => setDownloadPhase('idle')}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
        {entry.pull_enabled === true && (downloadPhase === 'idle' || downloadPhase === 'error') && (
          <>
            <button
              type="button"
              className="btn btn-primary guidance-download"
              data-testid="guidance-download"
              onClick={() => setDownloadPhase('confirm')}
            >
              <Download size={14} /> Download {PULL_MODEL}
            </button>
            {downloadPhase === 'error' && downloadError && (
              <p
                className="settings-inline-status settings-validation-message is-error"
                data-testid="guidance-download-error"
              >
                {downloadError}
              </p>
            )}
          </>
        )}
        {entry.pull_enabled === true && downloadPhase === 'active' && downloadPull && (
          <ModelPullProgress
            pull={downloadPull}
            onDone={() => { void onRecheck(); }}
          />
        )}
        <div className="guidance-command-row">
          <code className="guidance-command" data-testid="guidance-command">{PULL_COMMAND}</code>
          <button
            type="button"
            className="icon-btn"
            data-testid="guidance-copy-command"
            aria-label="Copy pull command"
            onClick={() => {
              void navigator.clipboard?.writeText(PULL_COMMAND).catch(() => undefined);
              setCopied(true);
            }}
          >
            {copied ? <Check size={12} /> : <Copy size={12} />}
          </button>
          {copied && <span className="local-server-guidance-copied">Copied</span>}
        </div>
      </>
    );
  } else {
    // empty_compat: no authoritative pull command exists for a generic
    // OpenAI-compatible server -- the fix lives in that server's own app.
    body = (
      <p className="local-server-guidance-copy">
        The server is running but has no model loaded. Load a model in your
        server app (LM Studio, vLLM, llama.cpp, or similar) on the machine
        that runs it, then Recheck.
      </p>
    );
  }

  return (
    <div
      className={`local-server-guidance local-server-guidance-${state} local-server-guidance-${variant}`}
      data-testid="local-server-guidance"
    >
      {body}
      {recheck}
      {recheckError && <p className="settings-inline-status is-error" role="alert">{recheckError}</p>}
    </div>
  );
}
