// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { WindowCloseButton } from '../../src/components/WindowCloseButton';

afterEach(cleanup);

describe('WindowCloseButton', () => {
  it('exposes its close purpose and delegates activation', async () => {
    const onClick = vi.fn();
    render(<WindowCloseButton label="Close report" onClick={onClick} />);

    const button = screen.getByRole('button', { name: 'Close report' });
    expect(button.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    await userEvent.click(button);

    expect(onClick).toHaveBeenCalledOnce();
  });

  it('prevents activation while disabled', async () => {
    const onClick = vi.fn();
    render(<WindowCloseButton label="Close busy dialog" disabled onClick={onClick} />);

    const button = screen.getByRole('button', { name: 'Close busy dialog' });
    expect(button).toBeDisabled();
    await userEvent.click(button);

    expect(onClick).not.toHaveBeenCalled();
  });
});
