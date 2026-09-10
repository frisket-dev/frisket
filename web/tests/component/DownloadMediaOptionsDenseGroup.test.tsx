// @vitest-environment jsdom
//
// download_media's yt-dlp throttle knobs are a denseGroup: compact
// number+unit fields packed into one `.dense-grid` instead of stacked
// `.param-row`s. Originally four (retries / socket-timeout / rate-limit /
// sleep-interval); Socket timeout and Rate limit were removed from the form
// per owner request ("sleep interval is fine and retries is fine"). Retries
// and Sleep interval are the two knobs left. This pins the dense markup AND
// that the existing per-field behaviour (clamp + wire testids) is unchanged.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import type { GeneratedActionParams } from '../../src/generated/actionTypes';
import { sheetMeta } from '../support/actionFormFixtures';
import { DownloadMediaOptions } from '../../src/components/action-panel/DownloadMediaOptions';



afterEach(cleanup);

const bodyContext = {
  sheet: sheetMeta([]), request: { scope: { kind: 'sheet_rows' as const, sheet_id: 1 } },
  errors: {}, Field: () => null,
};
type Params = GeneratedActionParams['media.ytdlp_download'];

const THROTTLE_FIELDS = [
  'youtube-adv-retries',
  'youtube-adv-sleep-interval',
] as const;

it('no longer renders the removed Rate limit / Socket timeout controls', () => {
  render(<DownloadMediaOptions {...bodyContext} params={{ source: 'url' }} setParams={vi.fn()} />);
  expect(screen.queryByTestId('youtube-adv-socket-timeout')).not.toBeInTheDocument();
  expect(screen.queryByTestId('youtube-adv-ratelimit')).not.toBeInTheDocument();
});

it('packs the remaining throttle knobs into one dense-grid, each a dense-grid-item', () => {
  render(<DownloadMediaOptions {...bodyContext} params={{ source: 'url' }} setParams={vi.fn()} />);
  const grid = screen.getByTestId('youtube-adv-throttle-grid');
  expect(grid).toHaveClass('dense-grid');
  for (const testid of THROTTLE_FIELDS) {
    const field = screen.getByTestId(testid);
    const item = field.closest('.dense-grid-item');
    expect(item).not.toBeNull();
    expect(item).toHaveAttribute('data-span', '1');
    expect(grid.contains(item)).toBe(true);
  }
});

it('preserves the clamp behaviour of a densified field', () => {
  // Apply the functional update synchronously inside the event — the handler
  // reads e.target.value there, before React reverts the controlled input.
  let captured: Params = { source: 'url' };
  const setParams = vi.fn((update: Params) => { captured = update; });
  render(<DownloadMediaOptions {...bodyContext} params={captured} setParams={setParams} />);
  // Retries clamps to [0, 20]; 99 must come back as 20.
  fireEvent.change(screen.getByTestId('youtube-adv-retries'), { target: { value: '99' } });
  expect(captured).toEqual({ source: 'url', extra_opts: { retries: 20 } });
});
