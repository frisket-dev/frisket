// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mockActionApiDefaults } from '../support/renderActionForm';
import { renderPythonForm } from '../support/pythonActionFixture';

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  mockActionApiDefaults();
});

describe('ActionForm help popover', () => {
  it('places help beside close and explains the current action', () => {
    renderPythonForm();

    const helpButton = screen.getByTestId('action-help-button');
    const closeButton = screen.getByTestId('action-drawer-close');
    const header = closeButton.closest('header');
    expect(header).not.toBeNull();
    const headerChildren = [...header!.children];
    expect(headerChildren.indexOf(helpButton)).toBe(headerChildren.indexOf(closeButton) - 1);
    expect(helpButton).toHaveAccessibleName('About Python');

    fireEvent.click(helpButton);
    const popover = screen.getByRole('dialog', { name: 'About Python' });
    expect(popover).toHaveTextContent('How it works');
    expect(popover).toHaveTextContent('Python');
    expect(popover).toHaveTextContent('What it needs');
    expect(popover).toHaveTextContent('What it produces');
    expect(popover).toHaveTextContent('Cost and requirements');
    expect(popover).toHaveTextContent('Good for');

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByTestId('action-help-popover')).not.toBeInTheDocument();
    expect(helpButton).toHaveFocus();
  });
});
