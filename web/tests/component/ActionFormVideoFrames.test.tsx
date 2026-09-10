// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { renderMediaAction } from '../support/renderMediaAction';

afterEach(cleanup);
const sheet = sheetMeta([columnDef({ id: '1', name: 'clip', type: 'video' })], { id: '7' });

describe('typed video frame sampling', () => {
  it('keeps visible count and interval controls with one canonical selection', async () => {
    const { onExecute } = renderMediaAction('media.video_frames', { sheet });
    expect(screen.getByTestId('generated-action-form')).toBeVisible();
    expect(screen.getByTestId('field-frame_count')).toHaveValue('4');
    fireEvent.click(screen.getByTestId('frame-sampling-interval'));
    expect(screen.getByTestId('field-interval_seconds')).toHaveValue('5');
    fireEvent.change(screen.getByTestId('field-interval_seconds'), { target: { value: '2.5' } });
    expect(screen.getByTestId('action-output-summary')).toHaveTextContent(/image blobs.*timestamp/i);
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toMatchObject({ action_id: 'media.video_frames',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'clip', sampling: { kind: 'interval', seconds: 2.5 }, max_dimension: 720 },
      output_names: { frames: 'frames' } });
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('frame_count');
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('interval_seconds');
  });

  it.each(['', '0', '201', '1.5', 'bad'])('host validation blocks invalid count %j, not the previous value', async (value) => {
    const { onExecute } = renderMediaAction('media.video_frames', { sheet });
    fireEvent.change(screen.getByTestId('field-frame_count'), { target: { value } });
    await waitFor(() => expect(screen.getByTestId('frame-sampling-error')).toBeVisible());
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(onExecute).not.toHaveBeenCalled();
  });
});
