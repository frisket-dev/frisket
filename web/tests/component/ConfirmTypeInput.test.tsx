// @vitest-environment jsdom
//
// "Type X to confirm" danger-zone/gate inputs (project delete, project
// compaction, the billable-run cost gate) are never browser-password-manager
// credentials, but a plain text input still reads as one to a browser's
// regular form-history autosuggest — clicking the field offered a
// saved-value dropdown from a prior fill. ConfirmTypeInput
// (src/components/PanelPrimitives.tsx) is the shared primitive every confirm
// field now goes through so autoComplete="off" is never something an
// individual danger-zone form has to remember.

import { useState } from 'react';
import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ConfirmTypeInput } from '../../src/components/PanelPrimitives';



afterEach(cleanup);

describe('ConfirmTypeInput', () => {
  it('opts out of form-history autosuggest and carries no password-shaped name/id', () => {
    render(
      <ConfirmTypeInput
        data-testid="confirm-input"
        value=""
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByTestId('confirm-input');
    expect(input).toHaveAttribute('autocomplete', 'off');
    expect(input).not.toHaveAttribute('name');
    expect(input).not.toHaveAttribute('id');
  });

  it('reports typed text back through the onChange(value) callback', async () => {
    const onChange = vi.fn();
    render(<ConfirmTypeInput data-testid="confirm-input" value="" onChange={onChange} />);

    await userEvent.type(screen.getByTestId('confirm-input'), 'CONFIRM');

    expect(onChange).toHaveBeenCalledWith('C');
    expect(onChange).toHaveBeenLastCalledWith('M');
  });

  function Controlled() {
    const [value, setValue] = useState('');
    return <ConfirmTypeInput data-testid="confirm-input" value={value} onChange={setValue} />;
  }

  it('does not block paste', async () => {
    render(<Controlled />);

    const input = screen.getByTestId('confirm-input') as HTMLInputElement;
    await userEvent.click(input);
    await userEvent.paste('COMPACT');

    expect(input.value).toBe('COMPACT');
  });

  it('still forwards caller-supplied id/className/placeholder for call sites that need them', () => {
    render(
      <ConfirmTypeInput
        id="cost-gate-input"
        className="form-input"
        placeholder="confirm"
        data-testid="confirm-input"
        value=""
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByTestId('confirm-input');
    expect(input).toHaveAttribute('id', 'cost-gate-input');
    expect(input).toHaveClass('form-input');
    expect(input).toHaveAttribute('placeholder', 'confirm');
    // The generic autoComplete="off" wins even when a caller supplies other props.
    expect(input).toHaveAttribute('autocomplete', 'off');
  });
});
