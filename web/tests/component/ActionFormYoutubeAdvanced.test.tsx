// @vitest-environment jsdom
//
// The action-owned yt-dlp controls publish semantic options directly into
// Params. The generated request host owns scope, output names, and submission.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { renderYtdlpForm } from '../support/renderYtdlpForm';

afterEach(cleanup);

function videosSheet() {
  return sheetMeta([columnDef({ id: '1', name: 'url', type: 'text' })], { id: '1' });
}

function requestExtraOpts(request: { params: Record<string, unknown> }): unknown {
  return request.params.extra_opts;
}

describe('youtube media advanced options', () => {
  let user: ReturnType<typeof userEvent.setup>;

  beforeEach(() => {
    user = userEvent.setup();
  });

  it('untouched Advanced options retains the declared null default without manufacturing options', async () => {
    const { onExecute } = renderYtdlpForm({
      sheet: videosSheet(),
    });
    await user.clear(await screen.findByTestId('field-output-video'));
    await user.type(screen.getByTestId('field-output-video'), 'media');

    // jest-dom's toBeVisible treats a closed <details> as not-visible (only
    // an `open` one passes its isAttributeVisible check) — real browsers
    // still render the closed summary row, so assert on that directly.
    const advanced = screen.getByTestId('advanced-group-yt-dlp-options');
    expect(advanced.querySelector('summary')).toHaveTextContent('Advanced options');

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(requestExtraOpts(onExecute.mock.calls[0][0])).toBeNull();
  });

  it('enabling subtitles + languages composes a scoped extra_opts payload', async () => {
    const { onExecute } = renderYtdlpForm({
      sheet: videosSheet(),
    });
    await user.clear(await screen.findByTestId('field-output-video'));
    await user.type(screen.getByTestId('field-output-video'), 'media');

    await user.click(screen.getByTestId('advanced-group-yt-dlp-options').querySelector('summary')!);
    expect(screen.getByTestId('youtube-adv-writesubtitles')).toBeVisible();
    await user.click(screen.getByTestId('youtube-adv-writesubtitles'));
    // Subtitle
    // languages is now a LabelChipsInput — type into its draft field and
    // commit with Enter (matching the chips-input's own commit contract,
    // ActionForm.tsx's LabelChipsInput) rather than a plain text field.
    await user.type(screen.getByTestId('youtube-adv-subtitleslangs-draft'), 'en{Enter}');
    expect(screen.getByTestId('youtube-adv-subtitleslangs-chip')).toHaveTextContent('en');

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(requestExtraOpts(onExecute.mock.calls[0][0])).toEqual({
      writesubtitles: true,
      subtitleslangs: ['en'],
    });
  });

  it('retries above the allowed bound is clamped client-side', async () => {
    const { onExecute } = renderYtdlpForm({
      sheet: videosSheet(),
    });
    await user.clear(await screen.findByTestId('field-output-video'));
    await user.type(screen.getByTestId('field-output-video'), 'media');

    await user.click(screen.getByTestId('advanced-group-yt-dlp-options').querySelector('summary')!);
    const retriesInput = screen.getByTestId('youtube-adv-retries');
    expect(retriesInput).toBeVisible();
    await user.type(retriesInput, '999');
    expect(retriesInput).toHaveValue('20');

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(requestExtraOpts(onExecute.mock.calls[0][0])).toEqual({ retries: 20 });
  });
});
