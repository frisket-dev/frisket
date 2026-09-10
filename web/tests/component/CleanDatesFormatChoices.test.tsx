// @vitest-environment jsdom
//
// Product contract for the clean_dates format chooser. A blank `format`
// delegates to the parser's deterministic Auto configuration; the UI exposes
// that alongside exact strftime presets and an explicit Custom path. This
// deliberately does not invent an ambiguity score, competing parse results,
// or an in-form review workflow.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
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

it('offers Auto-detect, seven strict presets, and Custom in one dropdown', () => {
  render(<Harness />);

  const chooser = screen.getByRole('combobox', { name: 'What do your dates look like?' });
  expect(chooser).toHaveValue('auto');
  expect(screen.queryAllByRole('radio')).toHaveLength(0);
  for (const [, label] of PRESET_OPTIONS) {
    expect(screen.getByRole('option', { name: label })).toBeInTheDocument();
  }
  expect(screen.getByRole('option', { name: 'Custom strftime' })).toBeInTheDocument();
  expect(screen.getAllByRole('option')).toHaveLength(PRESET_OPTIONS.length + 2);
});

it('writes a selected exact preset to the deterministic format parameter', () => {
  const onFormatChange = vi.fn();
  render(<Harness onFormatChange={onFormatChange} />);

  fireEvent.change(screen.getByTestId('clean-dates-format-select'), {
    target: { value: '%d/%m/%Y' },
  });
  expect(onFormatChange).toHaveBeenLastCalledWith('%d/%m/%Y');
  expect(screen.getByTestId('clean-dates-format-select')).toHaveValue('%d/%m/%Y');
});

it('uses the existing blank format as Auto-detect and only exposes free text through Custom', () => {
  const onFormatChange = vi.fn();
  render(<Harness onFormatChange={onFormatChange} />);

  expect(screen.queryByTestId('clean-dates-format-input')).toBeNull();
  const chooser = screen.getByTestId('clean-dates-format-select');
  fireEvent.change(chooser, { target: { value: 'custom' } });
  fireEvent.change(screen.getByTestId('clean-dates-format-input'), {
    target: { value: '%Y.%m.%d' },
  });
  expect(onFormatChange).toHaveBeenLastCalledWith('%Y.%m.%d');

  fireEvent.change(chooser, { target: { value: 'auto' } });
  expect(onFormatChange).toHaveBeenLastCalledWith('');
  expect(chooser).toHaveValue('auto');
  expect(screen.queryByTestId('clean-dates-format-input')).toBeNull();
});

it('does not add ambiguity-confidence, dual-parse, or review UI to this chooser', () => {
  render(<Harness />);

  // The operation already routes genuinely unparseable values to its normal
  // result handling. This chooser must not grow a competing, in-form flow.
  expect(screen.queryByTestId('clean-dates-ambiguity-confidence')).toBeNull();
  expect(screen.queryByTestId('clean-dates-dual-parse')).toBeNull();
  expect(screen.queryByTestId('clean-dates-review-workflow')).toBeNull();
});
