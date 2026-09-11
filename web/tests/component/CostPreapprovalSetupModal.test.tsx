// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '../../src/api/open';
import { CostPreapprovalSetupModal } from '../../src/components/CostPreapprovalSetupModal';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('CostPreapprovalSetupModal', () => {
  it('starts at $2 and records the amount with one click', async () => {
    const update = vi.spyOn(api, 'updateProfile').mockResolvedValue({
      email: 'owner@example.test',
      cost_preapproval_usd: '2',
    });
    const onComplete = vi.fn();
    render(<CostPreapprovalSetupModal onComplete={onComplete} />);

    expect(screen.getByTestId('cost-preapproval-setup-input')).toHaveValue(2);
    expect(screen.getByText(/sending their inputs to the selected provider/i)).toBeInTheDocument();
    expect(screen.queryByText(/type confirm/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('cost-preapproval-setup-save'));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ cost_preapproval_usd: '2' }),
    );
    await waitFor(() =>
      expect(screen.queryByTestId('cost-preapproval-setup')).not.toBeInTheDocument(),
    );
    expect(onComplete).toHaveBeenCalledOnce();
  });
});
