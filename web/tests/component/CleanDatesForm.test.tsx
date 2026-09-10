// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, within, render } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { CleanDatesForm } from '../../src/components/CleanDatesForm';



afterEach(cleanup);

const PRESET_OPTIONS = [
  ['%m/%d/%Y', '%m/%d/%Y — 03/14/2024'],
  ['%d/%m/%Y', '%d/%m/%Y — 14/03/2024'],
  ['%Y-%m-%d', '%Y-%m-%d — 2024-03-14'],
  ['%Y/%m/%d', '%Y/%m/%d — 2024/03/14'],
  ['%b %d, %Y', '%b %d, %Y — Mar 14, 2024'],
  ['%B %d, %Y', '%B %d, %Y — March 14, 2024'],
  ['%d %B %Y', '%d %B %Y — 14 March 2024'],
] as const;

function Harness({ onFormatChange }: { onFormatChange?: (value: string) => void }) {
  const [format, setFormat] = useState('');
  return (
    <CleanDatesForm
      format={format}
      onFormatChange={(value) => {
        setFormat(value);
        onFormatChange?.(value);
      }}
    />
  );
}

it('defaults the standard panel dropdown to Auto-detect with seven presets plus Custom', () => {
  render(<Harness />);

  const chooser = screen.getByRole('combobox', { name: 'What do your dates look like?' });
  expect(chooser).toHaveValue('auto');
  expect(screen.queryAllByRole('radio')).toHaveLength(0);
  expect(screen.getAllByRole('option')).toHaveLength(9);
  for (const [, label] of PRESET_OPTIONS) {
    expect(screen.getByRole('option', { name: label })).toBeInTheDocument();
  }
  expect(screen.getByRole('option', { name: 'Custom strftime' })).toBeInTheDocument();
  expect(screen.queryByTestId('clean-dates-format-input')).toBeNull();
});

it('writes the exact selected preset to the format param', () => {
  const onFormatChange = vi.fn();
  render(<Harness onFormatChange={onFormatChange} />);

  fireEvent.change(screen.getByTestId('clean-dates-format-select'), {
    target: { value: '%B %d, %Y' },
  });
  expect(onFormatChange).toHaveBeenLastCalledWith('%B %d, %Y');
  expect(screen.getByTestId('clean-dates-format-select')).toHaveValue('%B %d, %Y');
});

it('writes presets and opens Custom through the visible panel dropdown menu', () => {
  const onFormatChange = vi.fn();
  render(<Harness onFormatChange={onFormatChange} />);

  const chooser = screen.getByTestId('clean-dates-format-select');
  fireEvent.mouseDown(chooser);
  const presetMenu = screen.getByTestId('clean-dates-format-select-menu');
  fireEvent.click(
    within(presetMenu).getByRole('option', { name: '%d/%m/%Y — 14/03/2024' }),
  );
  expect(onFormatChange).toHaveBeenLastCalledWith('%d/%m/%Y');
  expect(chooser).toHaveValue('%d/%m/%Y');

  fireEvent.mouseDown(chooser);
  const customMenu = screen.getByTestId('clean-dates-format-select-menu');
  fireEvent.click(within(customMenu).getByRole('option', { name: 'Custom strftime' }));
  expect(chooser).toHaveValue('custom');
  expect(screen.getByTestId('clean-dates-format-input')).toHaveValue('%d/%m/%Y');
});

it('opens Custom from a preset and keeps it selected while editing', () => {
  const onFormatChange = vi.fn();
  render(<Harness onFormatChange={onFormatChange} />);

  const chooser = screen.getByTestId('clean-dates-format-select');
  fireEvent.change(chooser, { target: { value: '%m/%d/%Y' } });
  fireEvent.change(chooser, { target: { value: 'custom' } });
  expect(chooser).toHaveValue('custom');
  expect(screen.getByTestId('clean-dates-format-input')).toHaveValue('%m/%d/%Y');

  fireEvent.change(screen.getByTestId('clean-dates-format-input'), {
    target: { value: '%Y.%m.%d' },
  });
  expect(onFormatChange).toHaveBeenLastCalledWith('%Y.%m.%d');
  expect(chooser).toHaveValue('custom');
});

it('reveals free text only through Custom strftime and clears it through Auto-detect', () => {
  const onFormatChange = vi.fn();
  render(<Harness onFormatChange={onFormatChange} />);

  const chooser = screen.getByTestId('clean-dates-format-select');
  fireEvent.change(chooser, { target: { value: 'custom' } });
  const input = screen.getByTestId('clean-dates-format-input');
  fireEvent.change(input, { target: { value: '%Y.%m.%d' } });
  expect(onFormatChange).toHaveBeenLastCalledWith('%Y.%m.%d');
  expect(chooser).toHaveValue('custom');

  fireEvent.change(chooser, { target: { value: 'auto' } });
  expect(onFormatChange).toHaveBeenLastCalledWith('');
  expect(chooser).toHaveValue('auto');
  expect(screen.queryByTestId('clean-dates-format-input')).toBeNull();
});

it('does not expose the retired interpretation interface or an ambiguity workflow', () => {
  render(<Harness />);

  expect(document.body.textContent).not.toMatch(/dayfirst|month first|day first/i);
  expect(screen.queryByTestId('clean-dates-ambiguity-confidence')).toBeNull();
  expect(screen.queryByTestId('clean-dates-dual-parse')).toBeNull();
  expect(screen.queryByTestId('clean-dates-review-workflow')).toBeNull();
});
