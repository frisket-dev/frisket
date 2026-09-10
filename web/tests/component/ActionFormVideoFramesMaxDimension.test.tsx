// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import type { GeneratedActionDraft } from '../../src/api/types';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { renderMediaAction } from '../support/renderMediaAction';

afterEach(cleanup);
const sheet = sheetMeta([columnDef({ id: '1', name: 'clip', type: 'video' })], { id: '7' });
async function submit() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}

describe('typed video frame resize', () => {
  it('fresh UI visibly seeds720 and posts it', async () => {
    const { onExecute } = renderMediaAction('media.video_frames', { sheet });
    expect(screen.getByTestId('video-frames-max-dimension-select')).toHaveValue('720');
    await submit();
    expect(onExecute.mock.calls[0][0].params.max_dimension).toBe(720);
  });
  it('Original explicitly selects native-size null', async () => {
    const { onExecute } = renderMediaAction('media.video_frames', { sheet });
    await userEvent.selectOptions(screen.getByTestId('video-frames-max-dimension-select'), 'original');
    await submit();
    expect(onExecute.mock.calls[0][0].params.max_dimension).toBeNull();
  });
  it('Custom retains an editable preset and posts a valid replacement', async () => {
    const { onExecute } = renderMediaAction('media.video_frames', { sheet });
    await userEvent.selectOptions(screen.getByTestId('video-frames-max-dimension-select'), 'custom');
    expect(screen.getByTestId('field-max_dimension')).toHaveValue('720');
    fireEvent.change(screen.getByTestId('field-max_dimension'), { target: { value: '900' } });
    await submit();
    expect(onExecute.mock.calls[0][0].params.max_dimension).toBe(900);
  });
  it.each(['', '15', '5000'])('Custom %j is refused instead of silently becoming Original', async (value) => {
    const { onExecute } = renderMediaAction('media.video_frames', { sheet });
    await userEvent.selectOptions(screen.getByTestId('video-frames-max-dimension-select'), 'custom');
    fireEvent.change(screen.getByTestId('field-max_dimension'), { target: { value } });
    await waitFor(() => expect(screen.getByTestId('video-frames-max-dimension-error')).toHaveTextContent('16 and 4096'));
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(onExecute).not.toHaveBeenCalled();
  });
  it.each([
    { sampling: { kind: 'count', count: 6 } },
    { sampling: { kind: 'interval', seconds: 5 } },
    { sampling: { kind: 'count', count: 6 }, max_dimension: null },
    { sampling: { kind: 'count', count: 6 }, max_dimension: 1080 },
  ])('preserves saved sampling/resize and exact rows/names: %j', async (options) => {
    const initialDraft: GeneratedActionDraft = { action_id: 'media.video_frames',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [11, 13] },
      params: { source: 'clip', ...options }, output_names: { frames: 'Saved frames' } };
    const { onExecute, catalog } = renderMediaAction('media.video_frames', {
      sheet, initialDraft, selectedRowIds: ['11', '13'], hasExactRowScopeInitializer: true });
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, initialDraft))).toEqual(initialDraft);
    expect(screen.getByTestId('video-frames-max-dimension-select')).toHaveValue(
      options.max_dimension == null ? 'original' : String(options.max_dimension));
    await submit();
    expect(onExecute.mock.calls[0][0]).toEqual({ ...initialDraft, idempotency_key: expect.any(String) });
  });
  it('saved sampling and resize remain editable in the same canonical request', async () => {
    const { onExecute } = renderMediaAction('media.video_frames', { sheet, initialDraft: {
      action_id: 'media.video_frames', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'clip', sampling: { kind: 'count', count: 6 }, max_dimension: 1080 },
      output_names: { frames: 'Saved frames' } } });
    fireEvent.click(screen.getByTestId('frame-sampling-interval'));
    fireEvent.change(screen.getByTestId('field-interval_seconds'), { target: { value: '9' } });
    await userEvent.selectOptions(screen.getByTestId('video-frames-max-dimension-select'), '480');
    await submit();
    expect(onExecute.mock.calls[0][0].params).toEqual({
      source: 'clip', sampling: { kind: 'interval', seconds: 9 }, max_dimension: 480 });
  });
});
