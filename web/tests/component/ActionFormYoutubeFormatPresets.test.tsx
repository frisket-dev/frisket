// @vitest-environment jsdom
//
// `format_selector` becomes a preset select (Best audio/Best video/480p/720p/1080p/Best
// available) mapped to standard yt-dlp selector strings, with "Custom…"
// revealing the free-text field. Preset visibility respects the Audio/video
// mode toggle. Subtitle-language chips are covered here too since both
// live in the same form/disclosure.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { renderYtdlpForm } from '../support/renderYtdlpForm';

afterEach(cleanup);


function sheet() {
  return sheetMeta([columnDef({ id: '1', name: 'url', type: 'link' })], { id: '1' });
}


describe('youtube format presets (A14)', () => {
  it('defaults to Best available (null format_selector) and offers video presets in video mode', async () => {
    const { onExecute } = renderYtdlpForm({ sheet: sheet() });

    const formatSelect = screen.getByTestId('youtube-format-selector-select') as HTMLSelectElement;
    expect(formatSelect).toHaveValue('best_available');
    const optionLabels = Array.from(formatSelect.querySelectorAll('option')).map((o) => o.textContent);
    expect(optionLabels).toEqual(['Best available', 'Best video', '480p', '720p', '1080p', 'Custom…']);

    await userEvent.selectOptions(screen.getByTestId('field-source'), 'url');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.media_type',
      'video',
    );
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.format_selector',
      null,
    );
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('canonicalAction');
  });

  it('720p maps to the standard yt-dlp height-capped selector string', async () => {
    const { onExecute } = renderYtdlpForm({ sheet: sheet() });

    await userEvent.selectOptions(screen.getByTestId('youtube-format-selector-select'), '720p');
    await userEvent.selectOptions(screen.getByTestId('field-source'), 'url');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));

    // `?` (height<=?N) includes unknown-height
    // formats instead of dropping them outright.
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.format_selector',
      'bv*[height<=?720]+ba/b[height<=?720]',
    );
  });

  it('switching to Audio mode drops the resolution presets and offers Best audio instead', async () => {
    renderYtdlpForm({ sheet: sheet() });

    await userEvent.click(screen.getByTestId('youtube-media-type-audio'));
    const formatSelect = screen.getByTestId('youtube-format-selector-select') as HTMLSelectElement;
    const optionLabels = Array.from(formatSelect.querySelectorAll('option')).map((o) => o.textContent);
    expect(optionLabels).toEqual(['Best available', 'Best audio', 'Custom…']);
  });

  it('"Custom…" reveals the free-text yt-dlp expression field', async () => {
    const { onExecute } = renderYtdlpForm({ sheet: sheet() });

    expect(screen.queryByTestId('youtube-format-selector-input')).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByTestId('youtube-format-selector-select'), 'custom');
    const custom = screen.getByTestId('youtube-format-selector-input');
    await userEvent.type(custom, 'worst');

    await userEvent.selectOptions(screen.getByTestId('field-source'), 'url');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.format_selector',
      'worst',
    );
  });

  it('switching Best audio to Video mode remaps to Best video, not the stale audio-only selector', async () => {
    const { onExecute } = renderYtdlpForm({ sheet: sheet() });

    // Start in Audio mode with the Best audio preset selected.
    await userEvent.click(screen.getByTestId('youtube-media-type-audio'));
    await userEvent.selectOptions(screen.getByTestId('youtube-format-selector-select'), 'best_audio');

    // Switch back to Video — the picker must not keep shipping audio bytes
    // labeled media_type: 'video'.
    await userEvent.click(screen.getByTestId('youtube-media-type-video'));
    const formatSelect = screen.getByTestId('youtube-format-selector-select') as HTMLSelectElement;
    expect(formatSelect).toHaveValue('best_video');

    await userEvent.selectOptions(screen.getByTestId('field-source'), 'url');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.media_type',
      'video',
    );
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.format_selector',
      'bv*+ba/b',
    );
  });

  it('submits edited media controls when reopening a saved action', async () => {
    const { onExecute } = renderYtdlpForm({
      sheet: sheet(),
      initialDraft: {
        action_id: 'media.ytdlp_download', scope: { kind: 'sheet_rows', sheet_id: 1 },
        output_names: { audio: 'media' },
        params: {
          source: 'url',
          media_type: 'audio',
          format_selector: 'bestaudio/best',
        },
      },
    });

    expect(screen.getByTestId('youtube-media-type-select')).toHaveValue('audio');
    expect(screen.getByTestId('youtube-format-selector-select')).toHaveValue('best_audio');

    await userEvent.click(screen.getByTestId('youtube-media-type-video'));
    expect(screen.getByTestId('youtube-format-selector-select')).toHaveValue('best_video');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.media_type',
      'video',
    );
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.format_selector',
      'bv*+ba/b',
    );
  });

  it('switching a resolution-capped preset to Audio mode falls back to Best available', async () => {
    const { onExecute } = renderYtdlpForm({ sheet: sheet() });

    await userEvent.selectOptions(screen.getByTestId('youtube-format-selector-select'), '720p');
    await userEvent.click(screen.getByTestId('youtube-media-type-audio'));

    const formatSelect = screen.getByTestId('youtube-format-selector-select') as HTMLSelectElement;
    expect(formatSelect).toHaveValue('best_available');

    await userEvent.selectOptions(screen.getByTestId('field-source'), 'url');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.media_type',
      'audio',
    );
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.format_selector',
      null,
    );
  });

  it('a hand-typed Custom… selector survives a media_type switch untouched', async () => {
    const { onExecute } = renderYtdlpForm({ sheet: sheet() });

    await userEvent.selectOptions(screen.getByTestId('youtube-format-selector-select'), 'custom');
    await userEvent.type(screen.getByTestId('youtube-format-selector-input'), 'worst');

    await userEvent.click(screen.getByTestId('youtube-media-type-audio'));
    expect(screen.getByTestId('youtube-format-selector-select')).toHaveValue('custom');
    expect(screen.getByTestId('youtube-format-selector-input')).toHaveValue('worst');

    await userEvent.selectOptions(screen.getByTestId('field-source'), 'url');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toHaveProperty(
      'params.format_selector',
      'worst',
    );
  });

  it('A15: subtitle languages are chips, not a free-text field, with the availability hint', async () => {
    const { onExecute } = renderYtdlpForm({ sheet: sheet() });

    await userEvent.click(screen.getByTestId('advanced-group-yt-dlp-options').querySelector('summary')!);
    // Subtitle languages (and its availability hint) only render once
    // "Download subtitles" is on — they're meaningless otherwise.
    expect(screen.queryByText(/source platform makes them available/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('youtube-adv-writesubtitles'));
    expect(screen.getByText(/source platform makes them available/)).toBeVisible();

    const draft = screen.getByTestId('youtube-adv-subtitleslangs-draft');
    await userEvent.type(draft, 'en{Enter}');
    await userEvent.type(draft, 'es{Enter}');
    const chips = screen.getAllByTestId('youtube-adv-subtitleslangs-chip');
    expect(chips).toHaveLength(2);
    expect(chips.map((chip) => chip.textContent)).toEqual(['en', 'es']);

    await userEvent.selectOptions(screen.getByTestId('field-source'), 'url');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await userEvent.click(screen.getByTestId('generated-action-run'));

    const extraOpts = onExecute.mock.calls[0][0].params.extra_opts ?? {};
    expect(extraOpts.subtitleslangs).toEqual(['en', 'es']);
  });
});
