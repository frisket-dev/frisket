import { useState } from 'react';
import { getMediaProxyStatus } from '../api/open';
import { usePoll } from '../hooks/usePoll';
import './run-failure-triage.css';

const POLL_INTERVAL_MS = 5000;

type ChipState =
  | { kind: 'unknown' }
  | { kind: 'ok'; configured: boolean; connected: boolean | null };

/** Ambient media-proxy indicator for the media.ytdlp_download form: says
 *  BEFORE a run whether downloads will route through the server's media
 *  egress proxy, and warns when one is set but unreachable (those downloads
 *  fail with a connection error, not `youtube_provider_blocked`, so nothing
 *  else in the UI would explain them). Renders nothing when no proxy is
 *  configured or the status endpoint is unavailable (local tier 404) — the
 *  proxyless state is ordinary and earns no chrome. */
export function MediaProxyChip() {
  const [state, setState] = useState<ChipState>({ kind: 'unknown' });

  usePoll(
    async () => {
      try {
        const status = await getMediaProxyStatus();
        setState({
          kind: 'ok',
          configured: status.configured,
          connected: status.connected,
        });
      } catch {
        setState({ kind: 'unknown' });
      }
    },
    { intervalMs: POLL_INTERVAL_MS, active: true, guardOverlap: true, immediate: true },
  );

  if (state.kind !== 'ok' || !state.configured) return null;
  const connected = state.connected === true;
  return (
    <div
      className={`media-proxy-remediation-status ${connected ? 'is-connected' : 'is-disconnected'}`}
      data-testid="media-proxy-chip"
    >
      <span className="media-proxy-remediation-dot" />
      {connected
        ? 'Media proxy connected — downloads route through it'
        : 'Media proxy set but not reachable — downloads may fail until it reconnects'}
    </div>
  );
}
