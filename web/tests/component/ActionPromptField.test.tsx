// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ActionPromptField } from '../../src/components/action-panel/ActionPromptField';

afterEach(cleanup);

describe('ActionPromptField', () => {
  it('preserves the explicit field identity, shared classes, rows, and controlled change contract', () => {
    const onChange = vi.fn();
    render(
      <ActionPromptField
        label="How to summarize"
        value="Keep it concise"
        rows={3}
        onChange={onChange}
      />,
    );

    const textarea = screen.getByLabelText('How to summarize');
    expect(textarea).toBe(screen.getByTestId('action-prompt'));
    expect(textarea).toHaveAttribute('id', 'action-prompt');
    expect(textarea).toHaveAttribute('rows', '3');
    expect(textarea).toHaveClass('form-input', 'form-textarea', 'form-textarea-autogrow');
    expect(textarea).toHaveValue('Keep it concise');
    expect(screen.getByText('How to summarize')).toHaveClass('form-label');

    fireEvent.change(textarea, { target: { value: 'Lead with the finding' } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith('Lead with the finding');
  });

  it('sizes to its content on mount and on subsequent input', () => {
    const originalScrollHeight = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      'scrollHeight',
    );
    let scrollHeight = 72;
    Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    });

    try {
      render(
        <ActionPromptField
          label="Prompt"
          value="Initial"
          rows={5}
          onChange={() => {}}
        />,
      );

      const textarea = screen.getByTestId('action-prompt');
      expect(textarea).toHaveStyle({ height: '72px' });

      scrollHeight = 116;
      fireEvent.input(textarea);
      expect(textarea).toHaveStyle({ height: '116px' });
    } finally {
      if (originalScrollHeight) {
        Object.defineProperty(
          HTMLTextAreaElement.prototype,
          'scrollHeight',
          originalScrollHeight,
        );
      } else {
        delete (HTMLTextAreaElement.prototype as Partial<HTMLTextAreaElement>).scrollHeight;
      }
    }
  });

  it('renders an optional shared hint with its explicit test id', () => {
    render(
      <ActionPromptField
        label="Instructions"
        value=""
        rows={5}
        onChange={() => {}}
        hint="Describe what to pull out."
        hintTestId="extract-prompt-hint"
      />,
    );

    expect(screen.getByTestId('extract-prompt-hint'))
      .toHaveClass('form-hint');
    expect(screen.getByTestId('extract-prompt-hint'))
      .toHaveTextContent('Describe what to pull out.');
  });
});
