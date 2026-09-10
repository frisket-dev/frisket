// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ActionPreviewBanner } from '../../src/components/ActionPreviewBanner';
import type { PreviewGridView } from '../../src/state/previewViewStore';

afterEach(cleanup);

it.each([
  [null, 'unknown'], [0, '$0.00'], [0.0004, '$0.0004'],
] as const)('shows cancelled preview cost %s without claiming results were saved', (cost, label) => {
  const view: PreviewGridView = {
    sheetId: '1', previewId: 'paid', status: 'cancelled', progress: { done: 0, total: 1 },
    result: null, actionName: 'OCR', rowCount: 0, totalRows: 1, error: null,
    req: { action_id: 'media.ocr', scope: { kind: 'project' }, params: {}, output_names: {},
      idempotency_key: 'preview-paid' },
    accounting: { receipt_id: 'receipt-paid', status: 'cancelled', cost_actual: cost,
      elapsed_ms: 3200, model_call_count: 1 },
  };
  render(<ActionPreviewBanner view={view} onRun={vi.fn()} onClose={vi.fn()} />);
  expect(screen.getByTestId('preview-accounting').textContent).toBe(`Model cost: ${label} · 3.2s`);
  expect(screen.getByText('Preview cancelled')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Run for real' })).toBeNull();
});
