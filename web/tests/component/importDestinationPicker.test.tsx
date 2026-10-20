// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ImportDestinationPicker } from '../../src/components/importWorkspace/ImportDestinationPicker';

afterEach(cleanup);

describe('ImportDestinationPicker', () => {
  const sheets = [{ id: 7, name: 'Archive', rowCount: 1 }, { id: 9, name: 'Current', rowCount: 4 }];

  it('shows both fields and reports the preferred existing sheet without changing destination state', () => {
    const onDestination = vi.fn();
    render(<ImportDestinationPicker destination={{ kind: 'new', sheet_name: 'Contributions' }}
      sheets={sheets} defaultSheetName="Imported rows" preferredSheetId={9} busy={false}
      onDestination={onDestination} />);

    expect(screen.getByRole('group', { name: 'Import into' })).toBeVisible();
    expect(screen.getByRole('radio', { name: 'New sheet' })).toBeChecked();
    expect(screen.getByRole('textbox', { name: 'Sheet name' })).toBeEnabled();
    const existing = screen.getByRole('combobox', { name: 'Existing sheet' });
    expect(existing).toBeVisible();
    expect(existing).toBeDisabled();
    expect(existing).toHaveValue('9');
    expect(screen.getByRole('option', { name: 'Archive · 1 row' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Current · 4 rows' })).toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: 'Sheet name' }), { target: { value: 'Final name' } });
    expect(onDestination).toHaveBeenLastCalledWith({ kind: 'new', sheet_name: 'Final name' });
    fireEvent.click(screen.getByRole('radio', { name: 'Add to an existing sheet' }));
    expect(onDestination).toHaveBeenLastCalledWith({ kind: 'existing', sheet_id: 9 });
    expect(screen.getByRole('radio', { name: 'New sheet' })).toBeChecked();
  });

  it('edits an existing destination and returns to the flow-provided new sheet default', () => {
    const onDestination = vi.fn();
    render(<ImportDestinationPicker destination={{ kind: 'existing', sheet_id: 9 }}
      sheets={sheets} defaultSheetName="Imported files" busy={false} onDestination={onDestination} />);

    expect(screen.getByRole('radio', { name: 'Add to an existing sheet' })).toBeChecked();
    expect(screen.getByRole('textbox', { name: 'Sheet name' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: 'Sheet name' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'Sheet name' })).toHaveValue('Imported files');
    fireEvent.change(screen.getByRole('combobox', { name: 'Existing sheet' }), { target: { value: '7' } });
    expect(onDestination).toHaveBeenLastCalledWith({ kind: 'existing', sheet_id: 7 });
    fireEvent.click(screen.getByRole('radio', { name: 'New sheet' }));
    expect(onDestination).toHaveBeenLastCalledWith({ kind: 'new', sheet_name: 'Imported files' });
  });

  it('uses the first sheet when a preferred sheet is unavailable, without requiring row counts', () => {
    const onDestination = vi.fn();
    render(<ImportDestinationPicker destination={{ kind: 'new', sheet_name: 'Files' }}
      sheets={[{ id: 7, name: 'Archive' }]} defaultSheetName="Files" preferredSheetId={99}
      busy={false} onDestination={onDestination} />);
    expect(screen.getByRole('option', { name: 'Archive' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: 'Add to an existing sheet' }));
    expect(onDestination).toHaveBeenCalledWith({ kind: 'existing', sheet_id: 7 });
  });

  it('disables the existing destination when the project has no sheets', () => {
    const onDestination = vi.fn();
    render(<ImportDestinationPicker destination={{ kind: 'new', sheet_name: 'Files' }}
      sheets={[]} defaultSheetName="Files" busy={false} onDestination={onDestination} />);
    const existing = screen.getByRole('radio', { name: 'Add to an existing sheet' });
    expect(existing).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Existing sheet' })).toBeDisabled();
    expect(screen.getByRole('option', { name: 'No sheets yet' })).toBeInTheDocument();
    fireEvent.click(existing);
    expect(onDestination).not.toHaveBeenCalled();
  });

  it.each(['new', 'existing'] as const)('disables all destination controls while busy in %s mode', (kind) => {
    const onDestination = vi.fn();
    render(<ImportDestinationPicker destination={kind === 'new'
      ? { kind: 'new', sheet_name: 'Files' } : { kind: 'existing', sheet_id: 7 }}
      sheets={sheets} defaultSheetName="Files" busy onDestination={onDestination} />);
    for (const radio of screen.getAllByRole('radio')) expect(radio).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'Sheet name' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Existing sheet' })).toBeDisabled();
  });
});
