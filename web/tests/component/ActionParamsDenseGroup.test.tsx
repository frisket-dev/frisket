// @vitest-environment jsdom
//
// Generic renderer side of the denseGroup contract: consecutive params that
// share a `denseGroup` render as ONE `.dense-grid`, each param wrapped in a
// `.dense-grid-item` whose data-span comes from `denseSpan` (or is inferred —
// text/checkbox pack one track, selects/column pickers take two). Params
// outside the group interrupt and flush it, same as the checkbox grid.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import type { ActionParam } from '../../src/api/open';
import { ActionParams } from '../../src/components/action-panel/ActionParams';



afterEach(cleanup);

function renderParams(params: ActionParam[]) {
  return render(
    <ActionParams
      params={params}
      values={{}}
      columns={[]}
      onChange={vi.fn()}
    />,
  );
}

it('clusters same-denseGroup params into one dense-grid with inferred spans', () => {
  renderParams([
    { name: 'retries', label: 'Retries', input: 'text', denseGroup: 'throttle' },
    { name: 'mode', label: 'Mode', input: 'select', choices: ['a', 'b'], denseGroup: 'throttle' },
  ]);
  const grid = screen.getByTestId('dense-grid-throttle');
  expect(grid).toHaveClass('dense-grid');
  // Text default span 1, select default span 2.
  expect(screen.getByTestId('field-retries').closest('.dense-grid-item')).toHaveAttribute('data-span', '1');
  const modeItem = screen.getByTestId('field-mode').closest('.dense-grid-item');
  expect(modeItem).toHaveAttribute('data-span', '2');
  expect(grid.contains(modeItem)).toBe(true);
  // Dense items drop the .param-row wrapper (label stacks above control).
  expect(screen.getByTestId('field-retries').closest('.param-row')).toBeNull();
});

it('honours an explicit denseSpan and flushes on a non-dense param', () => {
  renderParams([
    { name: 'a', label: 'A', input: 'text', denseGroup: 'g', denseSpan: 2 },
    { name: 'plain', label: 'Plain', input: 'text' },
  ]);
  expect(screen.getByTestId('field-a').closest('.dense-grid-item')).toHaveAttribute('data-span', '2');
  // The plain param renders normally, outside any dense-grid.
  expect(screen.getByTestId('field-plain').closest('.dense-grid')).toBeNull();
  expect(screen.getByTestId('field-plain').closest('.param-row')).not.toBeNull();
});
