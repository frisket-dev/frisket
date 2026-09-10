// @vitest-environment jsdom
//
// MediaProxyRemediationCard: the youtube_provider_blocked remediation card
// RunFailureTriage renders above its generic buckets when a run's row
// errors include that code. Polls GET /org/media-proxy/status (usePoll,
// immediate) for a live connected indicator and gates the retry button on
// it; the local tier's 404 degrades to "assume I'm the operator, retry
// always enabled" rather than a dead end.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const getMediaProxyStatus = vi.hoisted(() => vi.fn());

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    getMediaProxyStatus,
    api: {
      ...actual.api,
      getMediaProxyStatus,
    },
  };
});

import { MediaProxyRemediationCard } from '../../src/components/MediaProxyRemediationCard';
import { RunFailureTriage } from '../../src/components/RunFailureTriage';
import { ApiError } from '../../src/api/open';
import type { RunRowErrorGroup, RunRowErrorSummary } from '../../src/api/types';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const blockedGroup = (over: Partial<RunRowErrorGroup> = {}): RunRowErrorGroup => ({
  message: 'YouTube blocked this download',
  count: 5,
  code: 'youtube_provider_blocked',
  outcome: 'model_error',
  terminal: false,
  rowIds: ['1', '2'],
  ...over,
});

describe('MediaProxyRemediationCard', () => {
  it('owner variant: shows the copyable command and copies it to the clipboard', async () => {
    getMediaProxyStatus.mockResolvedValue({ configured: false, connected: null, canConfigure: true });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<MediaProxyRemediationCard group={blockedGroup()} onRetryRows={vi.fn()} />);
    await waitFor(() => expect(getMediaProxyStatus).toHaveBeenCalled());

    expect(screen.getByText("YouTube blocked the server's network")).toBeInTheDocument();
    expect(screen.getByTestId('media-proxy-command')).toHaveTextContent('frisket proxy up');
    fireEvent.click(screen.getByTestId('media-proxy-copy-command'));
    expect(writeText).toHaveBeenCalledWith('frisket proxy up');
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  it('member variant: ask-your-admin phrasing, command still inline, no copy button', async () => {
    getMediaProxyStatus.mockResolvedValue({ configured: false, connected: null, canConfigure: false });

    render(<MediaProxyRemediationCard group={blockedGroup()} onRetryRows={vi.fn()} />);
    await waitFor(() => expect(getMediaProxyStatus).toHaveBeenCalled());

    expect(screen.getByText(/Ask your server admin/)).toBeInTheDocument();
    expect(screen.getByTestId('media-proxy-command')).toHaveTextContent('frisket proxy up');
    expect(screen.queryByTestId('media-proxy-copy-command')).not.toBeInTheDocument();
  });

  it('indicator flips to connected, enabling retry, which calls onRetryRows with the group outcome', async () => {
    getMediaProxyStatus.mockResolvedValue({ configured: true, connected: true, canConfigure: true });
    const onRetryRows = vi.fn();

    render(<MediaProxyRemediationCard group={blockedGroup({ outcome: 'model_error' })} onRetryRows={onRetryRows} />);
    await waitFor(() =>
      expect(screen.getByTestId('media-proxy-status-indicator')).toHaveTextContent('Proxy connected'),
    );

    const retry = screen.getByTestId('media-proxy-retry');
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    expect(onRetryRows).toHaveBeenCalledWith('model_error');
    expect(retry).toBeDisabled();
    expect(retry).toHaveTextContent('Retry requested');
  });

  it('not yet connected: indicator reads "not connected yet" and retry stays disabled', async () => {
    getMediaProxyStatus.mockResolvedValue({ configured: true, connected: false, canConfigure: true });

    render(<MediaProxyRemediationCard group={blockedGroup()} onRetryRows={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId('media-proxy-status-indicator')).toHaveTextContent('Proxy not connected yet'),
    );
    expect(screen.getByTestId('media-proxy-retry')).toBeDisabled();
  });

  it('404 (local tier): steps still show, indicator hidden, retry enabled and defaults to "any"', async () => {
    getMediaProxyStatus.mockRejectedValue(new ApiError(404, 'not found'));
    const onRetryRows = vi.fn();

    render(<MediaProxyRemediationCard group={blockedGroup({ outcome: null })} onRetryRows={onRetryRows} />);
    await waitFor(() => expect(getMediaProxyStatus).toHaveBeenCalled());

    expect(screen.queryByTestId('media-proxy-status-indicator')).not.toBeInTheDocument();
    expect(screen.getByTestId('media-proxy-command')).toBeInTheDocument();
    const retry = screen.getByTestId('media-proxy-retry');
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);
    expect(onRetryRows).toHaveBeenCalledWith('any');
  });

  it('omits the retry button entirely when onRetryRows is not provided', async () => {
    getMediaProxyStatus.mockResolvedValue({ configured: false, connected: null, canConfigure: true });
    render(<MediaProxyRemediationCard group={blockedGroup()} />);
    await waitFor(() => expect(getMediaProxyStatus).toHaveBeenCalled());
    expect(screen.queryByTestId('media-proxy-retry')).not.toBeInTheDocument();
  });
});

describe('RunFailureTriage media-proxy wiring', () => {
  const summary = (groups: RunRowErrorGroup[]): RunRowErrorSummary => ({
    totalFailedRows: groups.reduce((n, g) => n + g.count, 0),
    groups,
  });

  it('no card when no group carries youtube_provider_blocked', () => {
    getMediaProxyStatus.mockResolvedValue({ configured: false, connected: null, canConfigure: true });
    render(
      <RunFailureTriage
        rowErrors={summary([
          {
            message: 'provider rate limited',
            count: 3,
            code: 'provider_rate_limited',
            outcome: 'model_error',
            terminal: false,
            rowIds: ['1'],
          },
        ])}
      />,
    );
    expect(screen.queryByTestId('media-proxy-remediation-card')).not.toBeInTheDocument();
  });

  it('renders the card above the generic buckets when a group carries the code', async () => {
    getMediaProxyStatus.mockResolvedValue({ configured: false, connected: null, canConfigure: true });
    render(<RunFailureTriage rowErrors={summary([blockedGroup()])} onRetryRows={vi.fn()} />);
    await waitFor(() => expect(getMediaProxyStatus).toHaveBeenCalled());

    const triage = screen.getByTestId('run-failure-triage');
    expect(screen.getByTestId('media-proxy-remediation-card')).toBeInTheDocument();
    expect(triage.firstElementChild).toHaveAttribute('data-testid', 'media-proxy-remediation-card');
  });
});
