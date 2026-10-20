// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { OutputField } from '../../src/api/types';
import { StructuredFieldsEditor } from '../../src/components/action-panel/StructuredFieldsEditor';

afterEach(cleanup);

function Editor({ initial, onChange }: { initial: OutputField[]; onChange(fields: OutputField[]): void }) {
  const [fields, setFields] = useState(initial);
  return <StructuredFieldsEditor value={fields} onChange={(value) => {
    // The canonical field shape omits empty label arrays.
    const next = value.map(({ labels, ...field }) => ({ ...field,
      ...(labels?.length ? { labels } : {}) }));
    setFields(next);
    onChange(next);
  }} />;
}

it('keeps partially entered label text and focus when canonical optional fields are omitted', () => {
  render(<Editor initial={[{ name: 'category', type: 'category', description: '' }]} onChange={vi.fn()} />);
  const labels = screen.getByLabelText('Field 1 labels');
  labels.focus();
  fireEvent.change(labels, { target: { value: ', ' } });
  expect(screen.getByLabelText('Field 1 labels')).toBe(labels);
  expect(labels).toHaveFocus();
  expect(labels).toHaveValue(', ');
  fireEvent.change(labels, { target: { value: 'one, , two, ' } });
  expect(labels).toHaveValue('one, , two, ');
});

it('preserves nested list schemas while editing the field name', () => {
  const onChange = vi.fn();
  const items = { type: 'object' as const, properties: {
    status: { type: 'string' as const, enum: ['open', 'closed'] },
    nested: { type: 'object' as const, properties: { amount: { type: 'number' as const } } },
  }, required: ['status'] };
  render(<Editor initial={[{ name: 'items', type: 'list', description: '', items }]} onChange={onChange} />);
  fireEvent.change(screen.getByLabelText('Field 1 name'), { target: { value: 'records' } });
  expect(onChange.mock.lastCall?.[0][0]).toEqual({ name: 'records', type: 'list', description: '', items });
});

it('uses the same editable controls for nested fields without changing add/remove limits', () => {
  const onChange = vi.fn();
  render(<Editor initial={[{ name: 'items', type: 'list', description: '', items: {
    type: 'object', properties: { name: { type: 'string' } }, required: ['name'],
  } }]} onChange={onChange} />);
  expect(screen.getByTestId('list-item-field-delete')).toBeDisabled();
  fireEvent.click(screen.getByTestId('list-item-field-add'));
  expect(screen.getAllByTestId('list-item-field-name')).toHaveLength(2);
  fireEvent.change(screen.getAllByTestId('list-item-field-name')[1], { target: { value: 'city' } });
  expect(onChange.mock.lastCall?.[0][0].items.properties).toHaveProperty('city');
  fireEvent.click(screen.getAllByTestId('list-item-field-delete')[1]);
  expect(screen.getByTestId('list-item-field-delete')).toBeDisabled();
  expect(onChange.mock.lastCall?.[0][0].items.properties).not.toHaveProperty('city');
});
