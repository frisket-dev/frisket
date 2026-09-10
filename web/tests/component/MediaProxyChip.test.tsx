// @vitest-environment jsdom
//
// MediaProxyChip: the ambient proxy indicator on the download_media action
// form. Unlike the remediation card (which appears only after a
// youtube_provider_blocked failure), the chip answers "will this run route
// through a proxy?" BEFORE the run — and warns when a configured proxy is
// unreachable, the one state where downloads fail with nothing else in the
// UI to explain them. No proxy configured or no status endpoint (local
// tier 404) renders nothing at all.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, render } from '@testing-library/react';
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

import { MediaProxyChip } from '../../src/components/MediaProxyChip';
import { ApiError } from '../../src/api/open';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MediaProxyChip', () => {
  it('shows the connected state when a proxy is configured and reachable', async () => {
    getMediaProxyStatus.mockResolvedValue({
      configured: true,
      connected: true,
      canConfigure: true,
    });
    render(<MediaProxyChip />);
    const chip = await screen.findByTestId('media-proxy-chip');
    expect(chip).toHaveTextContent('Media proxy connected');
    expect(chip.className).toContain('is-connected');
  });

  it('warns when a proxy is configured but unreachable', async () => {
    getMediaProxyStatus.mockResolvedValue({
      configured: true,
      connected: false,
      canConfigure: false,
    });
    render(<MediaProxyChip />);
    const chip = await screen.findByTestId('media-proxy-chip');
    expect(chip).toHaveTextContent('not reachable');
    expect(chip.className).toContain('is-disconnected');
  });

  it('renders nothing when no proxy is configured', async () => {
    getMediaProxyStatus.mockResolvedValue({
      configured: false,
      connected: null,
      canConfigure: true,
    });
    render(<MediaProxyChip />);
    await waitFor(() => expect(getMediaProxyStatus).toHaveBeenCalled());
    expect(screen.queryByTestId('media-proxy-chip')).not.toBeInTheDocument();
  });

  it('renders nothing when the status endpoint is unavailable (local tier)', async () => {
    getMediaProxyStatus.mockRejectedValue(new ApiError(404, 'not found'));
    render(<MediaProxyChip />);
    await waitFor(() => expect(getMediaProxyStatus).toHaveBeenCalled());
    expect(screen.queryByTestId('media-proxy-chip')).not.toBeInTheDocument();
  });
});
