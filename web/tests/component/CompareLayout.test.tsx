// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { CompareEngineChip, CompareResultColumn, CompareToolbar } from '../../src/workbench/CompareLayout';

afterEach(cleanup);

it('puts engine selection before run controls in reading and keyboard order', async () => {
  const user = userEvent.setup();
  render(<CompareToolbar testIdPrefix="compare" running={false}
    controls={<button>Run</button>} note={<p>Usage is recorded</p>}>
    <button>Add engine</button>
  </CompareToolbar>);
  const toolbar = screen.getByTestId('compare-toolbar');
  expect([...toolbar.children].map((child) => child.textContent)).toEqual([
    'Temporary comparisonVariantsAdd engine', 'Run', 'Usage is recorded',
  ]);
  await user.tab();
  expect(screen.getByRole('button', { name: 'Add engine' })).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('button', { name: 'Run' })).toHaveFocus();
});

it('keeps body/configure/remove buttons distinct and keyboard-operable', async () => {
  const user = userEvent.setup();
  const onBodyClick = vi.fn();
  const onConfigure = vi.fn();
  const onRemove = vi.fn();
  render(<CompareEngineChip testIdPrefix="compare" engineId="local" label="Local engine"
    variantId="variant-1" pip="error" summary="English" onBodyClick={onBodyClick}
    onConfigure={onConfigure} onRemove={onRemove} />);
  expect(screen.getByTestId('compare-variant-chip')).toHaveAttribute('data-pip', 'error');
  expect(screen.getByText('Status: Failed')).toHaveClass('sr-only');
  await user.tab();
  await user.keyboard('{Enter}');
  expect(onBodyClick).toHaveBeenCalledOnce();
  await user.tab();
  expect(screen.getByRole('button', { name: 'Configure Local engine' }))
    .toHaveAttribute('data-icon-size', 'inline');
  await user.keyboard(' ');
  expect(onConfigure).toHaveBeenCalledOnce();
  await user.tab();
  expect(screen.getByRole('button', { name: 'Remove Local engine' })).toHaveFocus();
  expect(screen.getByRole('button', { name: 'Remove Local engine' }))
    .toHaveAttribute('data-icon-size', 'inline');
  await user.keyboard('{Enter}');
  expect(onRemove).toHaveBeenCalledOnce();
});

it.each([
  ['idle', 'Not run'],
  ['pending', 'Pending'],
  ['running', 'Running'],
  ['results', 'Results ready'],
  ['error', 'Failed'],
] as const)('names the %s pip state without exposing its visual glyph', (pip, status) => {
  render(<CompareEngineChip testIdPrefix="compare" engineId="local" label="Local engine"
    pip={pip} onRemove={() => undefined} />);
  const pipElement = screen.getByText(`Status: ${status}`).parentElement;
  expect(pipElement).toHaveAttribute('data-pip', pip);
  expect(pipElement?.firstElementChild).toHaveAttribute('aria-hidden', 'true');
});

it('preserves Transcribe native busy fieldset without disabling Cancel', async () => {
  const user = userEvent.setup();
  const onConfigure = vi.fn();
  const onRemove = vi.fn();
  const onCancel = vi.fn();
  render(<CompareToolbar testIdPrefix="transcribe-compare" running disabled
    controls={<button onClick={onCancel}>Cancel</button>}>
    <CompareEngineChip testIdPrefix="transcribe-compare" engineId="local" label="Local engine"
      onConfigure={onConfigure} onRemove={onRemove} />
  </CompareToolbar>);
  expect(screen.getByTestId('transcribe-compare-chips-row').tagName).toBe('FIELDSET');
  await user.click(screen.getByRole('button', { name: 'Configure Local engine' }));
  await user.click(screen.getByRole('button', { name: 'Remove Local engine' }));
  expect(onConfigure).not.toHaveBeenCalled();
  expect(onRemove).not.toHaveBeenCalled();
  await user.tab();
  expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  await user.keyboard('{Enter}');
  expect(onCancel).toHaveBeenCalledOnce();
});

it('keeps result metadata between its header and independently scrollable domain body', () => {
  render(<CompareResultColumn header={<button>Vote</button>} details={<p>Preview metadata</p>}
    textClassName="topic-compare-engine-text" data-testid="result" data-run-status="error">
    <div role="alert">Engine failed <button>Retry</button></div>
  </CompareResultColumn>);
  const result = screen.getByTestId('result');
  expect(result).toHaveAttribute('data-run-status', 'error');
  expect([...result.children].map((child) => child.textContent)).toEqual(['Vote', 'Preview metadata', 'Engine failed Retry']);
  expect(screen.getByRole('alert').parentElement).toHaveClass('ocr-compare-engine-text', 'topic-compare-engine-text');
});
