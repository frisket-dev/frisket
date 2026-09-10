// @vitest-environment jsdom
//
// Compact param
// widgets (text/select/number/checkbox/column pickers) render label-left,
// input-right via a shared `.param-row` wrapper instead of stacking label
// above input; textareas are the deliberate exception and stay stacked.
// Prompt/param textareas auto-grow with content instead of
// a fixed row count.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { ActionParams } from '../../src/components/action-panel/ActionParams';

afterEach(cleanup);

it('renders arbitrary unit metadata as a canonical generic param', () => {
  const onChange = vi.fn();
  render(
    <ActionParams
      params={[{
        name: 'payload_quota',
        label: 'Payload quota',
        input: 'text',
        unit: {
          defaultUnit: 'KB',
          units: [
            { value: 'B', multiplier: 1 },
            { value: 'KB', multiplier: 1000 },
          ],
        },
      }]}
      values={{ payload_quota: '2500' }}
      columns={[]}
      onChange={onChange}
    />,
  );

  const field = screen.getByTestId('field-payload_quota');
  const unit = screen.getByTestId('field-payload_quota-unit');
  expect(field).toHaveValue('2.5');
  expect(unit).toHaveValue('KB');
  fireEvent.change(unit, { target: { value: 'B' } });
  expect(field).toHaveValue('2500');
  fireEvent.change(field, { target: { value: '17' } });
  expect(onChange).toHaveBeenLastCalledWith('payload_quota', '17');
  fireEvent.change(field, { target: { value: 'malformed' } });
  expect(field).toHaveValue('malformed');
  expect(onChange).toHaveBeenLastCalledWith('payload_quota', '');
  fireEvent.change(unit, { target: { value: 'KB' } });
  fireEvent.change(field, { target: { value: '1.5' } });
  expect(onChange).toHaveBeenLastCalledWith('payload_quota', '1500');
});
