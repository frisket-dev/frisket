// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';

import { CompareRunControl } from '../../src/workbench/CompareRunControl';

afterEach(cleanup);

it('invokes the run callback from the idle presentation', async () => {
  const user = userEvent.setup();
  const onRun = vi.fn();
  render(<CompareRunControl testIdPrefix="compare" busy={false}
    onRun={onRun} onCancel={() => undefined} />);

  await user.click(screen.getByRole('button', { name: 'Run comparison' }));
  expect(onRun).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
});

it('guards a disabled run while exposing pending detail', async () => {
  const user = userEvent.setup();
  const onRun = vi.fn();
  render(<CompareRunControl testIdPrefix="compare" busy={false} disabled
    progress={<>3 pending</>}
    statusDetail="Add a comparison variant before running."
    onRun={onRun} onCancel={() => undefined} />);

  const run = screen.getByRole('button', { name: 'Run comparison' });
  expect(run).toBeDisabled();
  expect(run).toHaveAttribute('title', 'Add a comparison variant before running.');
  expect(screen.getByRole('status')).toHaveTextContent('3 pending');
  await user.click(run);
  expect(onRun).not.toHaveBeenCalled();
});

it('disables the status button during preparation while cancellation stays available', async () => {
  const user = userEvent.setup();
  const onRun = vi.fn();
  const onCancel = vi.fn();
  render(<CompareRunControl testIdPrefix="compare" busy disabled
    workingLabel="Estimating…" progress={<>Quoting 2 variants</>}
    statusDetail="Checking provider prices before starting."
    onRun={onRun} onCancel={onCancel} />);

  const status = screen.getByRole('button', { name: 'Estimating…' });
  expect(status).toBeDisabled();
  expect(status).toHaveAttribute('aria-busy', 'true');
  expect(screen.getByRole('status')).toHaveTextContent('Quoting 2 variants');
  expect(screen.getByRole('status')).toHaveAttribute('title', 'Checking provider prices before starting.');
  const cancel = screen.getByRole('button', { name: 'Cancel' });
  expect(cancel).toBeEnabled();
  await user.click(cancel);
  expect(onCancel).toHaveBeenCalledOnce();
  expect(onRun).not.toHaveBeenCalled();
});

it('reports running progress and invokes cancellation', async () => {
  const user = userEvent.setup();
  const onCancel = vi.fn();
  render(<CompareRunControl testIdPrefix="compare" busy
    progress={<>2 / 5 complete</>} onRun={() => undefined} onCancel={onCancel} />);

  expect(screen.getByRole('button', { name: 'Running…' })).toBeDisabled();
  expect(screen.getByTestId('compare-run-progress')).toHaveTextContent('2 / 5 complete');
  await user.click(screen.getByTestId('compare-cancel'));
  expect(onCancel).toHaveBeenCalledOnce();
});

it('supports a busy transport without inventing a cancellation action', () => {
  render(<CompareRunControl testIdPrefix="compare" busy
    workingLabel="Translating…" onRun={() => undefined} />);

  expect(screen.getByRole('button', { name: 'Translating…' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
});
