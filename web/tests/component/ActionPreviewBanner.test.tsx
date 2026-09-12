// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
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

it('shows the server preparation hint beside the existing zero-row preview status', () => {
  const view: PreviewGridView = {
    sheetId: '1', previewId: 'preparing', status: 'running',
    progress: {
      done: 0,
      total: 10,
      preparation: {
        message: 'Preparing the language model if this worker needs it, then translating…',
      },
    },
    result: null, actionName: 'Translate', rowCount: 0, totalRows: 10, error: null,
    req: { action_id: 'map.translate', scope: { kind: 'sheet_rows', sheet_id: 1 }, params: {},
      output_names: {}, idempotency_key: 'preview-preparing' },
  };
  render(<ActionPreviewBanner view={view} onRun={vi.fn()} onClose={vi.fn()} />);

  expect(screen.getByTestId('preview-preparation-hint')).toHaveTextContent(
    'Preparing the language model if this worker needs it, then translating…',
  );
  expect(screen.getByTestId('preview-tab-stats')).toHaveTextContent('Previewing… 0/10');
});
