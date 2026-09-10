// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { SelectionBar } from '../../src/components/resolve/SelectionBar';



afterEach(cleanup);

it('renders nothing while the selection is empty', () => {
  render(<SelectionBar count={0} onClear={() => {}} />);
  expect(screen.queryByTestId('resolve-selection-bar')).toBeNull();
});

it('shows the count, the caller actions, and a clear affordance', () => {
  const onClear = vi.fn();
  const onNewBucket = vi.fn();
  render(
    <SelectionBar count={2} onClear={onClear}>
      <button type="button" className="mini-btn" onClick={onNewBucket}>
        New bucket
      </button>
    </SelectionBar>,
  );

  expect(screen.getByTestId('resolve-selection-bar')).toBeInTheDocument();
  expect(screen.getByTestId('resolve-selection-count')).toHaveTextContent('2 selected');

  fireEvent.click(screen.getByRole('button', { name: 'New bucket' }));
  expect(onNewBucket).toHaveBeenCalledTimes(1);
  expect(onClear).not.toHaveBeenCalled();

  fireEvent.click(screen.getByTestId('resolve-selection-clear'));
  expect(onClear).toHaveBeenCalledTimes(1);
});

it('formats large counts with locale separators', () => {
  render(<SelectionBar count={2542} onClear={() => {}} />);
  expect(screen.getByTestId('resolve-selection-count')).toHaveTextContent(
    `${(2542).toLocaleString()} selected`,
  );
});
