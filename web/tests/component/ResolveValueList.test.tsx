// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, within, render } from '@testing-library/react';
import { useState, type ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { ValueList, type ValueListEntry } from '../../src/components/resolve/ValueList';



afterEach(cleanup);

const VALUES: ValueListEntry[] = [
  { value: 'ACME CORP.', count: 201 },
  { value: 'Acme Corp', count: 312 },
  { value: 'Hooli', count: 210 },
  { value: 'Acme, Inc.', count: 139 },
  { value: 'Umbrella Co', count: 88 },
  { value: 'GLOBEX', count: 81 },
];

function Harness({
  onChange,
  searchable = false,
  renderTrailing,
}: {
  onChange?: (next: Set<string>) => void;
  searchable?: boolean;
  renderTrailing?: (entry: ValueListEntry) => ReactNode;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  return (
    <ValueList
      values={VALUES}
      selected={selected}
      onSelectionChange={(next) => {
        setSelected(next);
        onChange?.(next);
      }}
      searchable={searchable}
      renderTrailing={renderTrailing}
    />
  );
}

function rowValues(): (string | null)[] {
  return screen.getAllByTestId('resolve-value-row').map((el) => el.getAttribute('data-value'));
}

function row(value: string): HTMLElement {
  const match = screen
    .getAllByTestId('resolve-value-row')
    .find((el) => el.getAttribute('data-value') === value);
  if (!match) throw new Error(`no visible row for ${value}`);
  return match;
}

it('renders rows frequency-sorted, count descending', () => {
  render(<Harness />);
  expect(rowValues()).toEqual([
    'Acme Corp',
    'Hooli',
    'ACME CORP.',
    'Acme, Inc.',
    'Umbrella Co',
    'GLOBEX',
  ]);
});

it('plain click toggles a row and accumulates across rows', () => {
  const onChange = vi.fn();
  render(<Harness onChange={onChange} />);

  fireEvent.click(row('Acme Corp'));
  expect(row('Acme Corp')).toHaveAttribute('aria-selected', 'true');

  fireEvent.click(row('Hooli'));
  expect(row('Acme Corp')).toHaveAttribute('aria-selected', 'true');
  expect(row('Hooli')).toHaveAttribute('aria-selected', 'true');
  expect(onChange).toHaveBeenLastCalledWith(new Set(['Acme Corp', 'Hooli']));

  fireEvent.click(row('Hooli'));
  expect(row('Hooli')).toHaveAttribute('aria-selected', 'false');
  expect(onChange).toHaveBeenLastCalledWith(new Set(['Acme Corp']));
});

it('shift-click selects the range from the last clicked row', () => {
  const onChange = vi.fn();
  render(<Harness onChange={onChange} />);

  fireEvent.click(row('Acme Corp'));
  fireEvent.click(row('Acme, Inc.'), { shiftKey: true });
  expect(onChange).toHaveBeenLastCalledWith(
    new Set(['Acme Corp', 'Hooli', 'ACME CORP.', 'Acme, Inc.']),
  );
});

it('cmd/ctrl-click toggles a single row without clearing the rest', () => {
  const onChange = vi.fn();
  render(<Harness onChange={onChange} />);

  fireEvent.click(row('Acme Corp'));
  fireEvent.click(row('Acme, Inc.'), { shiftKey: true });
  fireEvent.click(row('Hooli'), { metaKey: true });
  expect(onChange).toHaveBeenLastCalledWith(
    new Set(['Acme Corp', 'ACME CORP.', 'Acme, Inc.']),
  );
});

it('filters via the built-in search without losing the selection', () => {
  const onChange = vi.fn();
  render(<Harness onChange={onChange} searchable />);

  fireEvent.click(row('Hooli'));
  fireEvent.click(row('Acme Corp'));
  expect(onChange).toHaveBeenCalledTimes(2);

  fireEvent.change(screen.getByTestId('resolve-value-search'), {
    target: { value: 'acme' },
  });
  expect(rowValues()).toEqual(['Acme Corp', 'ACME CORP.', 'Acme, Inc.']);
  // Filtering is a view operation: no selection change fired, hidden rows
  // stay selected, visible selected rows stay marked.
  expect(onChange).toHaveBeenCalledTimes(2);
  expect(row('Acme Corp')).toHaveAttribute('aria-selected', 'true');

  fireEvent.change(screen.getByTestId('resolve-value-search'), {
    target: { value: '' },
  });
  expect(row('Hooli')).toHaveAttribute('aria-selected', 'true');
  expect(row('Acme Corp')).toHaveAttribute('aria-selected', 'true');
});

it('shows an empty note when the filter matches nothing', () => {
  render(<Harness searchable />);
  fireEvent.change(screen.getByTestId('resolve-value-search'), {
    target: { value: 'zzz' },
  });
  expect(screen.queryAllByTestId('resolve-value-row')).toHaveLength(0);
  expect(screen.getByTestId('resolve-value-empty')).toHaveTextContent('No values match "zzz"');
});

it('supports keyboard nav: arrows move the active row, Enter/Space toggle it', () => {
  const onChange = vi.fn();
  render(<Harness onChange={onChange} />);

  const listbox = screen.getByRole('listbox');
  fireEvent.keyDown(listbox, { key: 'ArrowDown' });
  fireEvent.keyDown(listbox, { key: 'Enter' });
  expect(onChange).toHaveBeenLastCalledWith(new Set(['Acme Corp']));

  fireEvent.keyDown(listbox, { key: 'ArrowDown' });
  fireEvent.keyDown(listbox, { key: ' ' });
  expect(onChange).toHaveBeenLastCalledWith(new Set(['Acme Corp', 'Hooli']));

  fireEvent.keyDown(listbox, { key: ' ' });
  expect(onChange).toHaveBeenLastCalledWith(new Set(['Acme Corp']));
});

it('clicks inside the trailing slot do not toggle selection', () => {
  const onChange = vi.fn();
  const onTrailing = vi.fn();
  render(
    <Harness
      onChange={onChange}
      renderTrailing={(entry) => (
        <button type="button" onClick={() => onTrailing(entry.value)}>
          add
        </button>
      )}
    />,
  );

  const trailingButton = row('Hooli').querySelector('button')!;
  fireEvent.click(trailingButton);
  expect(onTrailing).toHaveBeenCalledWith('Hooli');
  expect(onChange).not.toHaveBeenCalled();
  expect(row('Hooli')).toHaveAttribute('aria-selected', 'false');
});

it('marks leading/trailing whitespace so padded values are distinguishable', () => {
  const padded: ValueListEntry[] = [
    { value: ' Hooli', count: 10 },
    { value: 'Hooli ', count: 9 },
    { value: 'Hooli', count: 8 },
    { value: '  ', count: 1 },
  ];
  render(<ValueList values={padded} selected={new Set()} onSelectionChange={() => {}} />);

  const lead = within(row(' Hooli')).getByTestId('resolve-value-ws');
  expect(lead).toHaveAttribute('data-ws', 'leading');
  expect(lead.textContent).toBe(' ');

  const trail = within(row('Hooli ')).getByTestId('resolve-value-ws');
  expect(trail).toHaveAttribute('data-ws', 'trailing');
  expect(trail.textContent).toBe(' ');

  // An all-whitespace value renders one leading marker covering everything.
  const allWs = within(row('  ')).getByTestId('resolve-value-ws');
  expect(allWs).toHaveAttribute('data-ws', 'leading');
  expect(allWs.textContent).toBe('  ');

  // Clean values get no marker at all.
  expect(within(row('Hooli')).queryByTestId('resolve-value-ws')).toBeNull();
});

it('windows long lists instead of rendering every row', () => {
  const many: ValueListEntry[] = Array.from({ length: 500 }, (_, i) => ({
    value: `value_${i}`,
    count: 500 - i,
  }));
  render(
    <ValueList values={many} selected={new Set()} onSelectionChange={() => {}} />,
  );
  const rendered = screen.getAllByTestId('resolve-value-row').length;
  expect(rendered).toBeGreaterThan(0);
  expect(rendered).toBeLessThan(100);
});
