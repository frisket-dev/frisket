// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ActionFormHeader } from '../../src/components/action-panel/ActionFormHeader';
import { actionTemplateFor } from '../support/actionFormFixtures';

afterEach(cleanup);

const classify = actionTemplateFor('map.classify');
const summarize = actionTemplateFor('map.summarize');

function renderHeader(overrides: Partial<ComponentProps<typeof ActionFormHeader>> = {}) {
  const props: ComponentProps<typeof ActionFormHeader> = {
    actionTemplate: classify,
    sheetName: 'Interviews',
    variant: 'panel',
    onBack: vi.fn(),
    ...overrides,
  };
  return { ...render(<ActionFormHeader {...props} />), props };
}

describe('ActionFormHeader', () => {
  it('renders the panel title and sheet context and keeps the existing back callback', async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    renderHeader({ onBack, title: 'Review labels' });

    expect(screen.getByTestId('action-form-title')).toHaveTextContent('Review labels');
    expect(screen.getByTitle('on Interviews')).toHaveTextContent('on Interviews');
    await user.click(screen.getByRole('button', { name: 'Back to actions' }));
    expect(onBack).toHaveBeenCalledOnce();
  });

  it('renders the drawer affordances without a back button', async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    const onClose = vi.fn();
    const { container } = renderHeader({ variant: 'drawer', onBack, onClose });

    expect(container.querySelector('header')).toHaveClass('action-drawer-header');
    expect(screen.queryByRole('button', { name: 'Back to actions' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: `About ${classify.name}` })).toBeInTheDocument();
    await user.click(screen.getByTestId('action-drawer-close'));
    expect(onClose).toHaveBeenCalledOnce();
    expect(onBack).not.toHaveBeenCalled();
  });

  it('switches to a different action but only closes for the current action', async () => {
    const user = userEvent.setup();
    const onSwitchAction = vi.fn();
    renderHeader({ switchActions: [classify, summarize], onSwitchAction });

    const trigger = screen.getByTestId('action-title-menu-button');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('action-title-menu')).toBeInTheDocument();

    await user.click(screen.getByTestId('action-switch-map.classify'));
    expect(screen.queryByTestId('action-title-menu')).not.toBeInTheDocument();
    expect(onSwitchAction).not.toHaveBeenCalled();

    await user.click(trigger);
    await user.click(screen.getByTestId('action-switch-map.summarize'));
    expect(screen.queryByTestId('action-title-menu')).not.toBeInTheDocument();
    expect(onSwitchAction).toHaveBeenCalledOnce();
    expect(onSwitchAction).toHaveBeenCalledWith(summarize);
  });

  it('dismisses the switcher on outside pointer and focus events', async () => {
    const user = userEvent.setup();
    render(
      <>
        <ActionFormHeader
          actionTemplate={classify}
          sheetName="Interviews"
          switchActions={[classify, summarize]}
          onSwitchAction={vi.fn()}
          variant="panel"
          onBack={vi.fn()}
        />
        <button type="button">Outside</button>
      </>,
    );

    const trigger = screen.getByTestId('action-title-menu-button');
    await user.click(trigger);
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }));
    expect(screen.queryByTestId('action-title-menu')).not.toBeInTheDocument();

    await user.click(trigger);
    fireEvent.focusIn(screen.getByRole('button', { name: 'Outside' }));
    expect(screen.queryByTestId('action-title-menu')).not.toBeInTheDocument();
  });
});
