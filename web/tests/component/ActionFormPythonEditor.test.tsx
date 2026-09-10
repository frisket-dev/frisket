// @vitest-environment jsdom
//
// The Python recipe's code field uses a syntax-highlighted editor
// (react-simple-code-editor +
// prismjs), not a plain textarea.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';

import { renderPythonForm } from '../support/pythonActionFixture';

afterEach(cleanup);

it('Python recipe uses a syntax-highlighted code editor', () => {
  renderPythonForm();
  expect(screen.getByTestId('generated-action-form')).toBeVisible();

  const editor = screen.getByTestId('python-code-editor');
  expect(editor).toBeVisible();
  expect(editor).toHaveAttribute('data-language', 'python');
  // Prism emits <span class="token keyword"> (react-simple-code-editor +
  // prismjs); the default snippet has several keywords, so assert the first.
  expect(editor.querySelector('.token.keyword')).toHaveTextContent(/import|return|for|if|in/);
  expect(editor.querySelectorAll('textarea[data-testid="field-code"]')).toHaveLength(1);
  expect(editor.querySelectorAll('textarea.form-textarea[data-testid="field-code"]')).toHaveLength(0);
});
