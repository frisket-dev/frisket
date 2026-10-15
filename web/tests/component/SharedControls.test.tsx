// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { createRef, useState } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { Copy } from 'lucide-react';
import { IconButton } from '../../src/components/IconButton';
import { WindowCloseButton } from '../../src/components/WindowCloseButton';
import { PanelHeader } from '../../src/components/PanelPrimitives';
import { MultiColumnPicker } from '../../src/components/MultiColumnPicker';
import { LabelChipsInput } from '../../src/components/action-panel/LabelChipsInput';

afterEach(cleanup);

it('forwards native button semantics and refs without submitting a surrounding form', async () => {
  const ref = createRef<HTMLButtonElement>();
  const onClick = vi.fn();
  const onSubmit = vi.fn((event) => event.preventDefault());
  render(<form onSubmit={onSubmit}><IconButton ref={ref} icon={Copy} label="Copy value"
    title="Copy" aria-expanded={false} onClick={onClick} /></form>);
  const button = screen.getByRole('button', { name: 'Copy value' });
  expect(ref.current).toBe(button);
  expect(button).toHaveAttribute('title', 'Copy');
  expect(button).toHaveAttribute('aria-expanded', 'false');
  expect(button.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  button.focus();
  await userEvent.keyboard('{Enter}');
  expect(onClick).toHaveBeenCalledOnce();
  expect(onSubmit).not.toHaveBeenCalled();
});

it('preserves header close hooks and compact size, leaving dialog closes standard', async () => {
  const onClose = vi.fn();
  const ref = createRef<HTMLButtonElement>();
  render(<><PanelHeader onClose={onClose} closeAriaLabel="Close action" closeTestId="close-action"
    closeTitle="Close" closeClassName="layout-only" /><WindowCloseButton ref={ref} label="Close dialog" /></>);
  const button = screen.getByTestId('close-action');
  expect(button).toHaveAttribute('data-icon-size', 'compact');
  expect(button).toHaveClass('layout-only');
  expect(button).toHaveAccessibleName('Close action');
  await userEvent.click(button);
  expect(onClose).toHaveBeenCalledOnce();
  expect(ref.current).toBe(screen.getByRole('button', { name: 'Close dialog' }));
  expect(ref.current).toHaveAttribute('data-icon-size', 'standard');
});

it('removes a source chip without opening the picker or submitting; disabled chips cannot remove', async () => {
  const onValueChange = vi.fn();
  const onSubmit = vi.fn((event) => event.preventDefault());
  const props = { value: ['notes'], options: [{ name: 'notes', type: 'text' }], onValueChange };
  const { rerender } = render(<form onSubmit={onSubmit}><MultiColumnPicker {...props} /></form>);
  await userEvent.click(screen.getByRole('button', { name: 'Remove notes' }));
  expect(onValueChange).toHaveBeenCalledWith([]);
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(onSubmit).not.toHaveBeenCalled();
  onValueChange.mockClear();
  rerender(<MultiColumnPicker {...props} disabled />);
  await userEvent.click(screen.getByRole('button', { name: 'Remove notes' }));
  expect(onValueChange).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Remove notes' })).toBeDisabled();
});

it('keeps label entry, deduplication, and keyboard chip removal', async () => {
  function Labels() {
    const [value, setValue] = useState<readonly string[]>(['person']);
    return <LabelChipsInput typed value={value} onChange={setValue} placeholder="Labels" ariaLabel="Labels" testId="labels" />;
  }
  render(<Labels />);
  const input = screen.getByRole('textbox', { name: 'Labels' });
  await userEvent.type(input, 'ship,PERSON,{Enter}');
  expect(screen.getAllByTestId('labels-chip')).toHaveLength(2);
  await userEvent.keyboard('{Backspace}');
  expect(screen.queryByRole('button', { name: 'Remove ship' })).not.toBeInTheDocument();
  screen.getByRole('button', { name: 'Remove person' }).focus();
  await userEvent.keyboard(' ');
  expect(screen.queryAllByTestId('labels-chip')).toHaveLength(0);
});
