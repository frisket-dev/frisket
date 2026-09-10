// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { GroupCard, type GroupMember } from '../../src/components/resolve/GroupCard';



afterEach(cleanup);

const MEMBERS: GroupMember[] = [
  { value: 'Acme Corp', count: 312 },
  { value: 'ACME CORP.', count: 201 },
  { value: 'Acme, Inc.', count: 139 },
];

function Harness({
  initialName = 'Acme Corp',
  defaultName = 'Acme Corp',
  onNameChange,
  onDelete = () => {},
  onRemoveMember = () => {},
  autoFocusName = false,
}: {
  initialName?: string;
  defaultName?: string;
  onNameChange?: (name: string) => void;
  onDelete?: () => void;
  onRemoveMember?: (value: string) => void;
  autoFocusName?: boolean;
}) {
  const [name, setName] = useState(initialName);
  const [collapsed, setCollapsed] = useState(false);
  return (
    <GroupCard
      name={name}
      defaultName={defaultName}
      members={MEMBERS}
      collapsed={collapsed}
      onCollapsedChange={setCollapsed}
      onNameChange={(next) => {
        setName(next);
        onNameChange?.(next);
      }}
      onPromoteMember={(value) => {
        setName(value);
        onNameChange?.(value);
      }}
      onDelete={onDelete}
      onRemoveMember={onRemoveMember}
      autoFocusName={autoFocusName}
    />
  );
}

function nameInput(): HTMLInputElement {
  return screen.getByTestId('resolve-group-canonical-input');
}

it('commits an edited name on Enter', () => {
  const onNameChange = vi.fn();
  render(<Harness onNameChange={onNameChange} />);

  fireEvent.focus(nameInput());
  fireEvent.change(nameInput(), { target: { value: 'Acme Corporation' } });
  fireEvent.keyDown(nameInput(), { key: 'Enter' });

  expect(onNameChange).toHaveBeenCalledTimes(1);
  expect(onNameChange).toHaveBeenCalledWith('Acme Corporation');
  expect(nameInput()).toHaveValue('Acme Corporation');
});

it('reverts the draft on Escape without committing', () => {
  const onNameChange = vi.fn();
  render(<Harness onNameChange={onNameChange} />);

  fireEvent.focus(nameInput());
  fireEvent.change(nameInput(), { target: { value: 'zzz' } });
  fireEvent.keyDown(nameInput(), { key: 'Escape' });
  fireEvent.blur(nameInput());

  expect(onNameChange).not.toHaveBeenCalled();
  expect(nameInput()).toHaveValue('Acme Corp');
});

it('falls back to the default name when a confirmed name is empty', () => {
  const onNameChange = vi.fn();
  render(
    <Harness initialName="Acme Corporation" defaultName="Acme Corp" onNameChange={onNameChange} />,
  );

  fireEvent.focus(nameInput());
  fireEvent.change(nameInput(), { target: { value: '   ' } });
  fireEvent.keyDown(nameInput(), { key: 'Enter' });

  expect(onNameChange).toHaveBeenCalledWith('Acme Corp');
  expect(nameInput()).toHaveValue('Acme Corp');
});

it('commits on blur so tabbing away keeps the typed name', () => {
  const onNameChange = vi.fn();
  render(<Harness onNameChange={onNameChange} />);

  fireEvent.focus(nameInput());
  fireEvent.change(nameInput(), { target: { value: 'Acme Ltd' } });
  fireEvent.blur(nameInput());

  expect(onNameChange).toHaveBeenCalledTimes(1);
  expect(onNameChange).toHaveBeenCalledWith('Acme Ltd');
});

it('promotes a member to canonical via its diamond', () => {
  const onNameChange = vi.fn();
  render(<Harness onNameChange={onNameChange} />);

  const promotes = screen.getAllByTestId('resolve-group-member-promote');
  // The current canonical member's diamond is inert.
  expect(promotes[0]).toBeDisabled();
  expect(promotes[0]).toHaveAttribute('aria-pressed', 'true');

  fireEvent.click(promotes[1]);
  expect(onNameChange).toHaveBeenCalledWith('ACME CORP.');
  expect(nameInput()).toHaveValue('ACME CORP.');
  expect(screen.getAllByTestId('resolve-group-member-promote')[1]).toBeDisabled();
});

it('fires remove and delete callbacks', () => {
  const onDelete = vi.fn();
  const onRemoveMember = vi.fn();
  render(<Harness onDelete={onDelete} onRemoveMember={onRemoveMember} />);

  fireEvent.click(screen.getAllByTestId('resolve-group-member-remove')[2]);
  expect(onRemoveMember).toHaveBeenCalledWith('Acme, Inc.');

  fireEvent.click(screen.getByTestId('resolve-group-delete'));
  expect(onDelete).toHaveBeenCalledTimes(1);
});

it('collapses to a name · values · total summary and expands back', () => {
  render(<Harness />);
  expect(screen.getAllByTestId('resolve-group-member')).toHaveLength(3);
  expect(screen.getByTestId('resolve-group-total')).toHaveTextContent('652');

  fireEvent.click(screen.getByTestId('resolve-group-collapse'));
  expect(screen.queryByTestId('resolve-group-canonical-input')).toBeNull();
  expect(screen.queryAllByTestId('resolve-group-member')).toHaveLength(0);
  const summary = screen.getByTestId('resolve-group-summary');
  expect(summary).toHaveTextContent('Acme Corp');
  expect(summary).toHaveTextContent('3 values');
  expect(summary).toHaveTextContent('652');

  fireEvent.click(screen.getByTestId('resolve-group-collapse'));
  expect(screen.getAllByTestId('resolve-group-member')).toHaveLength(3);
});

it('auto-focuses the name input for freshly created groups', () => {
  render(<Harness autoFocusName />);
  expect(nameInput()).toHaveFocus();
});
