// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { GeneratedActionDraft } from '../../src/api/types';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { renderMediaAction } from '../support/renderMediaAction';

afterEach(cleanup);
const sheet = sheetMeta([
  columnDef({ id: '11', name: 'source', type: 'link' }),
  columnDef({ id: '12', name: 'text_urls', type: 'text' }),
  columnDef({ id: '13', name: 'category', type: 'category' }),
], { id: '7' });
async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}

describe('typed screenshot standard form', () => {
  it('launches canonical source, viewport, limits and names without a custom lifecycle', async () => {
    const { onExecute, entry } = renderMediaAction('web.capture_screenshot', { sheet, selectedRowIds: ['101'] });
    expect(entry.ui_hints.form).toBe('generated');
    expect(screen.getByTestId('field-full_page')).toBeChecked();
    expect(screen.getByTestId('field-viewport_width')).toHaveValue('1280');
    expect(screen.getByTestId('field-viewport_height')).toHaveValue('720');
    expect(screen.getByTestId('field-source')).not.toHaveTextContent('category');
    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'source' } });
    for (const [name, value] of Object.entries({ viewport_width: '1440', viewport_height: '900',
      max_bytes: '7500000', timeout_ms: '45500' })) {
      fireEvent.change(screen.getByTestId('field-' + name), { target: { value } });
    }
    fireEvent.click(screen.getByTestId('field-full_page'));
    fireEvent.change(screen.getByTestId('field-output-screenshot'), { target: { value: 'Saved screenshot' } });
    await run();
    expect(onExecute.mock.calls[0][0]).toEqual({
      action_id: 'web.capture_screenshot', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
      params: { source: 'source', full_page: false, viewport_width: 1440, viewport_height: 900,
        max_bytes: 7500000, timeout_ms: 45500 },
      output_names: { screenshot: 'Saved screenshot' }, idempotency_key: expect.any(String),
    });
    expect(screen.queryByTestId('field-render_mode')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-include_warc')).not.toBeInTheDocument();
  });

  it('saved MapRows keeps omitted fields omitted while displaying backend defaults', async () => {
    const draft: GeneratedActionDraft = { action_id: 'web.capture_screenshot',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 103] },
      params: { source: 'text_urls' }, output_names: { screenshot: 'Exact image' } };
    const { onExecute, catalog } = renderMediaAction('web.capture_screenshot', {
      sheet, initialDraft: draft, selectedRowIds: ['101', '103'], hasExactRowScopeInitializer: true });
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    expect(screen.getByTestId('field-full_page')).toBeChecked();
    expect(screen.getByTestId('field-viewport_width')).toHaveValue('1280');
    await run();
    expect(onExecute.mock.calls[0][0]).toEqual({ ...draft, idempotency_key: expect.any(String) });
  });

  it('changing one saved option does not materialize unrelated default Params', async () => {
    const { onExecute } = renderMediaAction('web.capture_screenshot', { sheet, initialDraft: {
      action_id: 'web.capture_screenshot', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'source', full_page: false }, output_names: { screenshot: 'Exact' },
    } });
    fireEvent.change(screen.getByTestId('field-timeout_ms'), { target: { value: '12000' } });
    await run();
    expect(onExecute.mock.calls[0][0].params).toEqual({ source: 'source', full_page: false, timeout_ms: 12000 });
  });
});
