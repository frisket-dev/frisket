// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '../../src/api/open';
import { AllowanceSetupModal } from '../../src/components/AllowanceSetupModal';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('AllowanceSetupModal', () => {
  it('starts at $2 and records the amount with one click', async () => {
    const update = vi.spyOn(api, 'updateProfile').mockResolvedValue({
      email: 'owner@example.test',
      allowance_usd: '2',
    });
    const onComplete = vi.fn();
    render(<AllowanceSetupModal onComplete={onComplete} />);

    expect(screen.getByTestId('allowance-setup-input')).toHaveValue(2);
    expect(screen.getByText(/sending their inputs to the selected provider/i)).toBeInTheDocument();
    expect(screen.queryByText(/type confirm/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('allowance-setup-save'));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ allowance_usd: '2' }),
    );
    await waitFor(() =>
      expect(screen.queryByTestId('allowance-setup')).not.toBeInTheDocument(),
    );
    expect(onComplete).toHaveBeenCalledOnce();
  });
});
