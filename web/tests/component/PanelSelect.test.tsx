// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRef, useState } from 'react';

import { PanelSelect } from '../../src/components/PanelSelect';

afterEach(cleanup);

describe('PanelSelect empty state', () => {
  it('shows the default non-selectable row when no options exist', () => {
    render(<PanelSelect value="" onValueChange={vi.fn()} options={[]} testId="empty-picker" />);

    fireEvent.mouseDown(screen.getByTestId('empty-picker'));
    const empty = within(screen.getByTestId('empty-picker-menu')).getByRole('option', {
      name: 'No items found',
    });
    expect(empty).toHaveAttribute('aria-disabled', 'true');
    expect(empty).toHaveAttribute('aria-selected', 'false');
    expect(empty.tagName).toBe('DIV');
  });

  it('lets callers explain what must be created first', () => {
    render(
      <PanelSelect
        value=""
        onValueChange={vi.fn()}
        options={[]}
        testId="ai-picker"
        emptyMessage="No AI columns yet — run an AI action first."
      />,
    );

    expect(screen.getByTestId('ai-picker')).toHaveTextContent(
      'No AI columns yet — run an AI action first.',
    );
    fireEvent.mouseDown(screen.getByTestId('ai-picker'));
    expect(
      within(screen.getByTestId('ai-picker-menu')).getByRole('option', {
        name: 'No AI columns yet — run an AI action first.',
      }),
    ).toHaveAttribute('aria-disabled', 'true');
  });
});

describe('PanelSelect native-option compatibility', () => {
  it('keeps the value-only callback used by rich-option call sites', () => {
    const changed = vi.fn();
    render(
      <PanelSelect
        value="one"
        onValueChange={changed}
        options={[
          { value: 'one', label: 'One' },
          { value: 'two', label: 'Two' },
        ]}
        testId="rich-picker"
      />,
    );

    fireEvent.mouseDown(screen.getByTestId('rich-picker'));
    fireEvent.click(within(screen.getByTestId('rich-picker-menu')).getByRole('option', { name: 'Two' }));

    expect(changed).toHaveBeenCalledWith('two');
  });

  it('renders option and optgroup children through the custom menu and reports a custom choice', () => {
    const changed = vi.fn();

    function Harness() {
      const [value, setValue] = useState('local');
      return (
        <PanelSelect
          data-testid="child-picker"
          aria-label="Engine"
          name="engine"
          value={value}
          onChange={(event) => {
            changed(event.currentTarget.value);
            setValue(event.currentTarget.value);
          }}
        >
          <optgroup label="Local">
            <option value="local">Local engine</option>
          </optgroup>
          <optgroup label="Hosted">
            <option value="hosted">Hosted engine</option>
          </optgroup>
        </PanelSelect>
      );
    }

    render(<Harness />);
    const trigger = screen.getByTestId('child-picker');
    expect(trigger).toHaveAttribute('name', 'engine');

    fireEvent.mouseDown(trigger);
    const menu = screen.getByTestId('child-picker-menu');
    expect(within(menu).getByText('Local')).toBeInTheDocument();
    expect(within(menu).getByText('Hosted')).toBeInTheDocument();
    fireEvent.click(within(menu).getByRole('option', { name: 'Hosted engine' }));

    expect(changed).toHaveBeenCalledWith('hosted');
    expect(trigger).toHaveValue('hosted');
  });

  it('forwards the underlying select ref used by existing focus and e2e adapters', () => {
    const ref = createRef<HTMLSelectElement>();
    render(
      <PanelSelect ref={ref} value="one" onValueChange={vi.fn()}>
        <option value="one">One</option>
      </PanelSelect>,
    );

    expect(ref.current).toBeInstanceOf(HTMLSelectElement);
    expect(ref.current).toHaveValue('one');
  });
});
