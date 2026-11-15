// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ReapplyResultsDialog } from '../../src/components/review/ReapplyResultsDialog';
import type { ReviewRun } from '../../src/api/types';
import { installDialogPolyfill } from '../support/domPolyfills';

const { listSheets, runAction } = vi.hoisted(() => ({ listSheets: vi.fn(), runAction: vi.fn() }));
vi.mock('../../src/bind/useWorkspaceStores', () => {
  const stores = { projectApi: { listSheets, runAction } };
  return { useWorkspaceStores: () => stores };
});
const run = { runId: '9', sheetId: '2', outputs: [
  { key: 'answer', columnId: '4', columnName: 'Answer', columnType: 'text' },
  { key: 'confidence', columnId: '5', columnName: 'Confidence', columnType: 'number' },
] } as ReviewRun;
beforeEach(() => {
  installDialogPolyfill();
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  listSheets.mockResolvedValue([{ id: '2', columns: [
    { id: '4', name: 'Answer', type: 'text', ai: {}, generationManaged: true },
    { id: '5', name: 'Confidence', type: 'number', ai: {}, generationManaged: true },
    { id: '6', name: 'Answer_2', type: 'text' },
  ] }]);
  runAction.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('names every saved role independently without overwriting, including support outputs', async () => {
  render(<ReapplyResultsDialog run={run} onClose={vi.fn()} onApplied={vi.fn()} />);
  await waitFor(() => expect(screen.getByTestId('reapply-output-answer')).toHaveValue('Answer_3'));
  expect(screen.getByTestId('reapply-output-confidence')).toHaveValue('Confidence_2');
  fireEvent.click(screen.getByRole('button', { name: 'Reapply results' }));
  await waitFor(() => expect(runAction).toHaveBeenCalledWith(expect.objectContaining({
    replace_existing: false, output_names: { answer: 'Answer_3', confidence: 'Confidence_2' },
    expected_output_columns: { Answer_3: null, Confidence_2: null },
  })));
});
it('replaces only explicitly selected destinations with their expected identity', async () => {
  render(<ReapplyResultsDialog run={run} onClose={vi.fn()} onApplied={vi.fn()} />);
  const input = await screen.findByTestId('reapply-output-answer');
  fireEvent.change(input, { target: { value: 'Answer' } });
  expect(screen.getByRole('status')).toHaveTextContent('replace the current values in Answer');
  fireEvent.click(screen.getByRole('button', { name: 'Reapply results' }));
  await waitFor(() => expect(runAction).toHaveBeenCalledWith(expect.objectContaining({
    replace_existing: true, output_names: { answer: 'Answer', confidence: 'Confidence_2' },
    expected_output_columns: { Answer: 4, Confidence_2: null },
  })));
});
it('blocks duplicate or blank destinations', async () => {
  render(<ReapplyResultsDialog run={run} onClose={vi.fn()} onApplied={vi.fn()} />);
  fireEvent.change(await screen.findByTestId('reapply-output-answer'), { target: { value: 'Confidence_2' } });
  expect(screen.queryByRole('button', { name: 'Reapply results' })).not.toBeInTheDocument();
  expect(runAction).not.toHaveBeenCalled();
});
