// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ComparisonReview, type ComparisonReviewProps } from '../../src/workbench/evaluation/ComparisonReview';

afterEach(cleanup);
function props(): ComparisonReviewProps {
  return { fieldLabel: 'amount', rowId: 7, position: 1, total: 2, mode: 'disagreements',
    groups: [{ key: 'one', answerText: '$100', variantKeys: ['A', 'B'], verdict: 'not_checked' },
      { key: 'two', answerText: '$200', variantKeys: ['C'], verdict: 'not_checked' }],
    expectedValue: '', rule: 'exact', busy: false, source: <div>Source document</div>,
    onExpectedChange: vi.fn(), onRuleChange: vi.fn(), onAccept: vi.fn(), onReject: vi.fn(),
    onPrevious: vi.fn(), onNext: vi.fn(), onClose: vi.fn() };
}
it('shows grouped answers and keeps rejection separate from expected answer', () => {
  const p = props(); render(<ComparisonReview {...p} />);
  expect(screen.getByText('Source document')).toBeVisible();
  expect(screen.getByRole('button', { name: /Answer 1, variants A, B/ })).toBeVisible();
  expect(screen.getByRole('region', { name: 'Distinct answers' })).not.toContainElement(screen.getByLabelText('Expected answer'));
  fireEvent.click(screen.getByRole('button', { name: 'Reject answer 1' }));
  expect(p.onReject).toHaveBeenCalledWith('one'); expect(p.onExpectedChange).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Accept answer 2' }));
  expect(p.onAccept).toHaveBeenCalledWith('two');
  fireEvent.change(screen.getByLabelText('Expected answer'), { target: { value: '300' } });
  expect(p.onExpectedChange).toHaveBeenCalledWith('300');
  fireEvent.change(screen.getByLabelText('Match rule'), { target: { value: 'ignore_case' } });
  expect(p.onRuleChange).toHaveBeenCalledWith('ignore_case');
});
it('labels a local rejected candidate as ruled out before any expected answer exists', () => {
  const p = props(); p.groups[0].verdict = 'ruled_out';
  render(<ComparisonReview {...p} />);
  expect(screen.getByRole('button', { name: /Answer 1, variants A, B, Ruled out/ })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Reject answer 1' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.queryByText('Miss')).toBeNull();
});
it('supports navigation and decisions with Review shortcuts while ignoring typing', () => {
  const p = props(); render(<ComparisonReview {...p} />);
  fireEvent.keyDown(window, { key: 's' }); fireEvent.keyDown(window, { key: 'a' });
  expect(p.onAccept).toHaveBeenCalledWith('two');
  fireEvent.keyDown(window, { key: 'd' }); expect(p.onReject).toHaveBeenCalledWith('two');
  fireEvent.keyDown(screen.getByLabelText('Expected answer'), { key: 'a' });
  expect(p.onAccept).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getAllByRole('button', { name: 'Next row' })[0]); expect(p.onNext).toHaveBeenCalled();
});
it('offers expected editing in scoring mode and disables actions while busy', () => {
  const p = { ...props(), mode: 'score' as const, expectedValue: '100' };
  const { rerender } = render(<ComparisonReview {...p} />);
  expect(screen.queryByLabelText('Expected answer')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Edit expected answer' }));
  expect(screen.getByLabelText('Expected answer')).toHaveValue('100');
  rerender(<ComparisonReview {...p} busy />);
  expect(screen.getByRole('button', { name: 'Accept answer 1' })).toBeDisabled();
});

it('renders bounded values without offering to copy or edit an excerpt', () => {
  const p = props();
  const field = { id: 'f', runId: '', sheetId: '2', rowId: '7', columnId: '4', columnName: 'amount',
    columnType: 'text' as const, value: null, confidence: null, justification: '', reviewState: 'unreviewed' as const,
    role: 'field' as const, chore: true, boundedValue: { preview: 'Partial answer', totalCharacters: 90000, totalBytes: 90000, downloadHref: null } };
  p.groups[0].field = field; p.expectedField = field;
  render(<ComparisonReview {...p} />);
  expect(screen.getAllByText('Partial answer')).toHaveLength(2);
  expect(screen.getByRole('button', { name: 'Accept answer 1' })).toBeDisabled();
  expect(screen.queryByLabelText('Expected answer')).not.toBeInTheDocument();
});
