// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import type { ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReviewBundle, ReviewBundleField, RunTraceRowEvidence } from '../../src/api/types';
import { ReviewResearch } from '../../src/components/review/ReviewResearch';

const { api } = vi.hoisted(() => ({ api: { getRunTraceRow: vi.fn() } }));
const { getRunTraceRow } = api;
vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi: api }),
}));
vi.mock('../../src/components/Modal', () => ({
  Modal: ({ children }: { children: ReactNode }) => <div role="dialog">{children}</div>,
}));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const answer: ReviewBundleField = {
  id: '7:1:4', runId: '7', rowId: '1', sheetId: '2', columnId: '4',
  columnName: 'Answer', columnType: 'text', value: 'Unknown', outputKey: 'answer',
  confidence: null, justification: '', reviewState: 'unreviewed', role: 'field', chore: true,
};
const source = {
  ...answer, id: '7:1:5', columnId: '5', columnName: 'Links', outputKey: 'sources',
  value: '["https://example.org/source"]', role: 'field' as const,
};
const bundle: ReviewBundle = {
  id: '7:1', runId: '7', rowId: '1', sheetId: '2', sheetName: 'Companies', rowIndex: 0,
  actionKind: 'research.answer', actionName: 'Agent Research', model: 'test',
  confidence: null, source: {}, context: '', fields: [answer, source],
  evidence: [], items: [answer, source],
};

it('shows renamed historical companion sources before trace loading and after failure', async () => {
  getRunTraceRow.mockRejectedValue(new Error('unavailable'));
  render(<ReviewResearch bundle={bundle} field={answer} />);
  expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.org/source');
  expect(await screen.findByText('Research activity could not be loaded.')).toBeVisible();
  expect(screen.getByRole('link')).toBeVisible();
  expect(getRunTraceRow).toHaveBeenCalledWith('7', '1', '4');
});

it('ignores a late response after switching the keyed row owner', async () => {
  let finishFirst!: (value: RunTraceRowEvidence) => void;
  getRunTraceRow.mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }))
    .mockRejectedValueOnce(new Error('second failed'));
  const view = render(<ReviewResearch key="first" bundle={bundle} field={answer} />);
  view.rerender(<ReviewResearch key="second" bundle={{ ...bundle, rowId: '2' }} field={{ ...answer, rowId: '2' }} />);
  await screen.findByText('Research activity could not be loaded.');
  await act(async () => { finishFirst({
    runId: '7', rowId: '1', columnId: '4', status: 'missing', recorded: false, trace: null,
  }); });
  expect(screen.queryByRole('button', { name: 'Research details' })).not.toBeInTheDocument();
  expect(screen.getByText('Research activity could not be loaded.')).toBeVisible();
});

it('does not treat a bounded Review excerpt as an empty exact-run answer', async () => {
  getRunTraceRow.mockResolvedValue({
    runId: '7', rowId: '1', columnId: '4', status: 'not_recorded', recorded: false,
    trace: null, cell: { value: 'The exact-run answer is available here.', error: null },
  });
  render(<ReviewResearch bundle={bundle} field={{ ...answer, value: null,
    boundedValue: { preview: 'The exact-run…', totalCharacters: 5000, totalBytes: 5000, downloadHref: null },
  }} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Research details' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('The exact-run answer is available here.');
  expect(screen.queryByText('No result recorded for this cell.')).not.toBeInTheDocument();
});

it('shows the shared source list alongside an arbitrary structured research field', async () => {
  getRunTraceRow.mockRejectedValue(new Error('unavailable'));
  const founded = { ...answer, outputKey: 'founded', columnName: 'Founded', columnType: 'integer', value: 1984 };
  render(<ReviewResearch bundle={{ ...bundle, actionKind: 'research.extract', fields: [founded],
    items: [founded, source] }} field={founded} />);
  expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.org/source');
  expect(await screen.findByText('Research activity could not be loaded.')).toBeVisible();
  expect(getRunTraceRow).toHaveBeenCalledWith('7', '1', '4');
});
