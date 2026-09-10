import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { getMediaProxyStatus, type RunRowErrorGroup } from '../api/open';
import { usePoll } from '../hooks/usePoll';
import './run-failure-triage.css';

const PROXY_COMMAND = 'frisket proxy up';
const POLL_INTERVAL_MS = 4000;

// The status probe is org-scoped and 404s on the local tier (no org identity
// to ask). A 404 or any other probe failure is treated the same as "the
// caller must be the one who runs the proxy" — the operator variant (steps,
// retry always enabled) never dead-ends behind a broken indicator.
type ProbeState =
  | { kind: 'pending' }
  | { kind: 'unavailable' }
  | { kind: 'ok'; configured: boolean; connected: boolean | null; canConfigure: boolean };

export function MediaProxyRemediationCard({
  group,
  onRetryRows,
}: {
  /** The youtube_provider_blocked row-error group RunFailureTriage found. */
  group: RunRowErrorGroup;
  /** Retry exactly this group's rows (run.backfill row_ids), same prop
   *  RunFailureTriage's own buckets use. */
  onRetryRows?: (outcome: string) => void;
}) {
  const [probe, setProbe] = useState<ProbeState>({ kind: 'pending' });
  const [copied, setCopied] = useState(false);
  const [retried, setRetried] = useState(false);

  usePoll(
    async () => {
      try {
        const status = await getMediaProxyStatus();
        setProbe({
          kind: 'ok',
          configured: status.configured,
          connected: status.connected,
          canConfigure: status.canConfigure,
        });
      } catch {
        // The local tier (no org identity) answers 404 here; other probe
        // failures (network error, 5xx) degrade the same way — neither the
        // operator variant nor the retry affordance depends on a live
        // status read, so there is nothing to gain by branching on status.
        setProbe({ kind: 'unavailable' });
      }
    },
    { intervalMs: POLL_INTERVAL_MS, active: true, guardOverlap: true, immediate: true },
  );

  const connected = probe.kind === 'ok' ? probe.connected : null;
  const canConfigure = probe.kind === 'ok' ? probe.canConfigure : true;
  const retryEnabled = probe.kind === 'unavailable' || connected === true;
  const outcome = group.outcome ?? 'any';

  const copyCommand = () => {
    void navigator.clipboard?.writeText(PROXY_COMMAND).catch(() => undefined);
    setCopied(true);
  };

  const retry = () => {
    if (!onRetryRows) return;
    setRetried(true);
    onRetryRows(outcome);
  };

  const command = (
    <code className="guidance-command" data-testid="media-proxy-command">
      {PROXY_COMMAND}
    </code>
  );

  return (
    <div className="media-proxy-remediation" data-testid="media-proxy-remediation-card">
      <div className="media-proxy-remediation-head">
        <span className="media-proxy-remediation-title">
          YouTube blocked the server's network
        </span>
        <p className="media-proxy-remediation-subline">
          You can route downloads through your own computer instead.
        </p>
      </div>
      {canConfigure ? (
        <ol className="media-proxy-remediation-steps">
          <li>
            On your own computer, run:
            <div className="guidance-command-row">
              {command}
              <button
                type="button"
                className="icon-btn"
                data-testid="media-proxy-copy-command"
                aria-label="Copy proxy command"
                onClick={copyCommand}
              >
                {copied ? <Check size={12} /> : <Copy size={12} />}
              </button>
              {copied && <span className="local-server-guidance-copied">Copied</span>}
            </div>
          </li>
          <li>
            Leave it running — it lends the server your network connection
            while downloads retry.
          </li>
        </ol>
      ) : (
        <p className="media-proxy-remediation-copy">
          Ask your server admin to run {command} — it routes media downloads
          through their network connection.
        </p>
      )}
      {probe.kind === 'ok' && (
        <div
          className={`media-proxy-remediation-status ${connected ? 'is-connected' : 'is-disconnected'}`}
          data-testid="media-proxy-status-indicator"
        >
          <span className="media-proxy-remediation-dot" />
          {connected ? 'Proxy connected' : 'Proxy not connected yet'}
        </div>
      )}
      {onRetryRows && (
        <button
          type="button"
          className="mini-btn"
          data-testid="media-proxy-retry"
          disabled={!retryEnabled || retried}
          onClick={retry}
        >
          {retried ? 'Retry requested' : 'Retry failed rows'}
        </button>
      )}
    </div>
  );
}
