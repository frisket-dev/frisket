// @vitest-environment jsdom
//
// "Include auto-generated subtitles" and "Subtitle languages" are meaningless
// with "Download subtitles" off — they used to always render regardless.
// Owner ask: hide both sub-options until "Download subtitles" is toggled on.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { GeneratedActionParams } from '../../src/generated/actionTypes';
import { sheetMeta } from '../support/actionFormFixtures';
import { DownloadMediaOptions } from '../../src/components/action-panel/DownloadMediaOptions';



afterEach(cleanup);

const bodyContext = {
  sheet: sheetMeta([]), request: { scope: { kind: 'sheet_rows' as const, sheet_id: 1 } },
  errors: {}, Field: () => null,
};
type Params = GeneratedActionParams['media.ytdlp_download'];

const SUB_OPTION_TESTIDS = ['youtube-adv-writeautomaticsub', 'youtube-adv-subtitleslangs'] as const;

describe('download_media subtitle sub-options', () => {
  it('hides the sub-options while Download subtitles is off (default state)', () => {
    render(<DownloadMediaOptions {...bodyContext} params={{ source: 'url' }} setParams={vi.fn()} />);
    expect(screen.getByTestId('youtube-adv-writesubtitles')).toBeInTheDocument();
    for (const testid of SUB_OPTION_TESTIDS) {
      expect(screen.queryByTestId(testid)).not.toBeInTheDocument();
    }
    expect(screen.queryByText('Subtitle languages')).not.toBeInTheDocument();
  });

  it('hides the sub-options when Download subtitles is explicitly off', () => {
    render(
      <DownloadMediaOptions {...bodyContext} params={{ source: 'url', extra_opts: { writesubtitles: false } }} setParams={vi.fn()} />,
    );
    for (const testid of SUB_OPTION_TESTIDS) {
      expect(screen.queryByTestId(testid)).not.toBeInTheDocument();
    }
  });

  it('reveals both sub-options once Download subtitles is on', () => {
    render(
      <DownloadMediaOptions {...bodyContext} params={{ source: 'url', extra_opts: { writesubtitles: true } }} setParams={vi.fn()} />,
    );
    expect(screen.getByTestId('youtube-adv-writeautomaticsub')).toBeInTheDocument();
    expect(screen.getByTestId('youtube-adv-subtitleslangs')).toBeInTheDocument();
    expect(screen.getByText('Subtitle languages')).toBeInTheDocument();
  });

  it('reveals the sub-options live when the toggle is clicked on', () => {
    let params: Params = { source: 'url' };
    const setParams = vi.fn((update: Params) => { params = update; });
    const { rerender } = render(
      <DownloadMediaOptions {...bodyContext} params={params} setParams={setParams} />,
    );
    expect(screen.queryByTestId('youtube-adv-writeautomaticsub')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('youtube-adv-writesubtitles'));
    expect(params).toEqual({ source: 'url', extra_opts: { writesubtitles: true } });
    rerender(<DownloadMediaOptions {...bodyContext} params={params} setParams={setParams} />);

    expect(screen.getByTestId('youtube-adv-writeautomaticsub')).toBeInTheDocument();
    expect(screen.getByTestId('youtube-adv-subtitleslangs')).toBeInTheDocument();
  });

  it('preserves saved hidden options and explicit false values when editing a visible control', () => {
    const params: Params = { source: 'url', extra_opts: {
      allsubtitles: true, socket_timeout: 15, ratelimit: 1000,
      writeautomaticsub: false, subtitlesformat: 'vtt/srt/best',
    } };
    const setParams = vi.fn();
    render(<DownloadMediaOptions {...bodyContext} params={params} setParams={setParams} />);
    fireEvent.click(screen.getByTestId('youtube-adv-writethumbnail'));
    expect(setParams).toHaveBeenCalledWith({ ...params,
      extra_opts: { ...params.extra_opts, writethumbnail: true },
    });
  });
});
