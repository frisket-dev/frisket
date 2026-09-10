// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { OutputNameCombobox } from '../../src/components/action-panel/TargetSaveToControl';
import { columnDef } from '../support/domainFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';

beforeAll(installPopoverPolyfill);

afterEach(cleanup);

const columns = [
  columnDef({ id: 'col-headline', name: 'headline', type: 'text' }),
  columnDef({ id: 'col-body', name: 'body', type: 'text' }),
  columnDef({ id: 'col-topic', name: 'topic', type: 'text' }),
];

describe('OutputNameCombobox native and rich-popup contract', () => {
  it('uses the visible label, implicit role, and label activation', async () => {
    const user = userEvent.setup();
    render(
      <OutputNameCombobox columns={columns} value="topic" onChange={vi.fn()} />,
    );

    const input = screen.getByTestId('new-column-name');
    expect(input).toHaveAccessibleName('Save to');
    expect(input).toHaveAttribute('list', 'new-column-name-options');
    expect(input).toHaveAttribute('aria-controls', 'new-column-name-listbox');
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).not.toHaveAttribute('role');
    expect(input).toHaveAttribute('aria-autocomplete', 'list');

    await user.click(screen.getByText('Save to'));
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute('aria-expanded', 'true');
  });

  it('tracks one active option across keyboard and pointer without moving DOM focus', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });
    render(
      <>
        <OutputNameCombobox columns={columns} value="" onChange={onChange} />
        <button type="button">Outside</button>
      </>,
    );

    const input = screen.getByTestId('new-column-name');
    await user.click(input);
    await user.keyboard('{ArrowDown}');
    const firstActiveId = input.getAttribute('aria-activedescendant');
    expect(firstActiveId).toBeTruthy();
    expect(document.getElementById(firstActiveId!)).toHaveClass('is-active');
    expect(input).toHaveFocus();

    await user.keyboard('{ArrowDown}');
    const secondActiveId = input.getAttribute('aria-activedescendant');
    expect(secondActiveId).toBeTruthy();
    expect(secondActiveId).not.toBe(firstActiveId);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });

    await user.keyboard('{ArrowUp}');
    expect(input).toHaveAttribute('aria-activedescendant', firstActiveId);

    const bodyOption = screen.getByTestId('new-column-name-option-body');
    fireEvent.mouseEnter(bodyOption);
    expect(input).toHaveAttribute('aria-activedescendant', bodyOption.id);
    expect(input).toHaveFocus();
    expect(bodyOption).toHaveAttribute('tabindex', '-1');

    await user.keyboard('{Enter}');
    expect(onChange).toHaveBeenLastCalledWith('body');
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).toHaveFocus();

    await user.click(input);
    await user.keyboard('{Escape}');
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(onChange).toHaveBeenCalledTimes(1);

    await user.click(input);
    await user.tab();
    expect(screen.getByRole('button', { name: 'Outside' })).toHaveFocus();
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('preserves callback timing for direct entry and pointer selection', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <>
        <OutputNameCombobox columns={columns} value="" onChange={onChange} />
        <button type="button">Outside</button>
      </>,
    );

    const input = screen.getByTestId('new-column-name');
    fireEvent.change(input, { target: { value: 'draft' } });
    expect(onChange).toHaveBeenLastCalledWith('draft');
    fireEvent.click(input);
    await user.click(screen.getByTestId('new-column-name-option-topic'));
    expect(onChange).toHaveBeenLastCalledWith('topic');

    await user.click(input);
    await user.click(screen.getByRole('button', { name: 'Outside' }));
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });
});
