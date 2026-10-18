// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ActivityProgressCard, ProgressBar, ProgressRow } from '../../src/components/ActivityProgress';

afterEach(cleanup);

describe('shared activity progress presentation', () => {
  it('uses explicit state instead of treating a full count as success', () => {
    render(
      <ProgressRow
        label="OCR"
        completed={3}
        total={3}
        state="error"
        status="3 of 3 · needs attention"
        ariaLabel="OCR progress"
      />,
    );

    const row = screen.getByText('OCR').closest('.activity-progress-row');
    expect(row).toHaveClass('status-error');
    expect(row).not.toHaveClass('status-complete');
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '3');
  });

  it('animates an unknown total only for active work', () => {
    const view = render(
      <ProgressBar label="Active setup" completed={null} total={null} state="active" />,
    );
    expect(screen.getByRole('progressbar')).toHaveClass('indeterminate');

    view.rerender(
      <ProgressBar label="Stopped setup" completed={null} total={null} state="warning" />,
    );
    expect(screen.getByRole('progressbar')).not.toHaveClass('indeterminate');
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
  });

  it('gives active cards a visible liveness indicator', () => {
    render(
      <ActivityProgressCard state="active" title="Preparing files" detail="Waiting for counts" />,
    );
    expect(screen.getByRole('status').querySelector('.run-spinner')).toBeInTheDocument();
  });
});
