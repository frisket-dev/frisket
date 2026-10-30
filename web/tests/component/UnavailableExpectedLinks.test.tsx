// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { EvaluationLink } from '../../src/api/evaluation';
import { UnavailableExpectedLinks } from '../../src/workspace/UnavailableExpectedLinks';
afterEach(cleanup);
it('shows the reason and allows removing a broken link', () => {
  const remove = vi.fn();
  const link = { id: 4, expected_sheet_name: 'Answers', unavailable_reason: 'Expected sheet was removed' } as EvaluationLink;
  render(<UnavailableExpectedLinks links={[link]} onRemove={remove} />);
  expect(screen.getByText(/Expected sheet was removed/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Remove link' }));
  expect(remove).toHaveBeenCalledWith(4);
});
