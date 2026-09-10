// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { CleanDatesForm } from '../../src/components/CleanDatesForm';



afterEach(cleanup);

const OPTIONS = [
  'Auto-detect',
  '%m/%d/%Y — 03/14/2024',
  '%d/%m/%Y — 14/03/2024',
  '%Y-%m-%d — 2024-03-14',
  '%Y/%m/%d — 2024/03/14',
  '%b %d, %Y — Mar 14, 2024',
  '%B %d, %Y — March 14, 2024',
  '%d %B %Y — 14 March 2024',
  'Custom strftime',
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

it('uses the panel dropdown idiom for date-format choices and preserves exact wire values', () => {
  const onFormatChange = vi.fn();
  render(<Harness onFormatChange={onFormatChange} />);

  const chooser = screen.getByRole('combobox', { name: /What do your dates look like\?/i });
  expect(screen.queryAllByRole('radio')).toHaveLength(0);
  expect(screen.getAllByRole('option')).toHaveLength(OPTIONS.length);
  for (const option of OPTIONS) {
    expect(screen.getByRole('option', { name: option })).toBeInTheDocument();
  }

  fireEvent.change(chooser, { target: { value: '%d/%m/%Y' } });
  expect(onFormatChange).toHaveBeenLastCalledWith('%d/%m/%Y');
});
