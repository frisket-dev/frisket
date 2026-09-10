// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { OutputField } from '../../src/api/types';
import { StructuredFieldsEditor } from '../../src/components/action-panel/StructuredFieldsEditor';

afterEach(cleanup);

it('cycles the shared extraction field required control and updates canonical field values', async () => {
  const onChange = vi.fn();
  function Editor() {
    const [fields, setFields] = useState<OutputField[]>([]);
    return <StructuredFieldsEditor value={fields} onChange={(value) => {
      setFields(value);
      onChange(value);
    }} />;
  }
  render(<Editor />);
  await userEvent.click(screen.getByRole('button', { name: 'Add column' }));
  const toggle = screen.getAllByTestId('output-field-required')[0];
  expect(toggle).toHaveAttribute('aria-pressed', 'false');
  expect(toggle).not.toHaveClass('is-required');
  expect(toggle).toHaveTextContent('*');
  const originalField = onChange.mock.lastCall![0][0];

  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-pressed', 'true');
  expect(toggle).toHaveClass('is-required');
  expect(toggle).toHaveAttribute(
    'title', 'Required — the model must find this or the field is flagged',
  );
  expect(onChange.mock.lastCall![0]).toEqual([{ ...originalField, required: true }]);

  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-pressed', 'false');
  expect(toggle).not.toHaveClass('is-required');
  expect(onChange.mock.lastCall![0]).toEqual([originalField]);
});
