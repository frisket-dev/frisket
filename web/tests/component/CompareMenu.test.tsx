// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CompareMenu } from '../../src/workbench/CompareMenu';
afterEach(cleanup);
it('shows real runs and eligibility reasons, and opens the expected comparison', () => {
  const onCompareRun = vi.fn();
  const anchor = { sheetId: '1', runId: '2', columnId: '3' };
  render(<CompareMenu runs={[{ anchor, label: 'Ask', expected: { sheetName: 'Gold', rowCount: 3 } }]} actions={[{ kind: 'map.ask', label: 'Ask rows', group: 'rows', disabledReason: 'Needs text' }]} onCompareRun={onCompareRun} onCompareAction={vi.fn()} onJoinExpected={vi.fn()} />);
  fireEvent.click(screen.getByTestId('compare-menu-trigger'));
  expect(screen.getByRole('menuitem', { name: /Ask rows/ })).toBeDisabled();
  expect(screen.getByText('Needs text')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('menuitem', { name: /Ask vs expected/ }));
  expect(onCompareRun).toHaveBeenCalledWith(anchor, true);
});
it('keeps expected answers out of the media menu', () => {
  render(<CompareMenu media runs={[]} actions={[]} onCompareRun={vi.fn()} onCompareAction={vi.fn()} onJoinExpected={vi.fn()} />);
  fireEvent.click(screen.getByTestId('compare-menu-trigger'));
  expect(screen.queryByText('EXPECTED ANSWERS')).not.toBeInTheDocument();
});
it('offers Compare choices inside the compact menu and dismisses after selection', () => {
  const close = vi.fn(); const action = vi.fn();
  render(<div role="menu"><CompareMenu inline onDismiss={close} runs={[]} actions={[{ kind: 'map.ask', label: 'Ask rows', group: 'rows' }]} onCompareRun={vi.fn()} onCompareAction={action} onJoinExpected={vi.fn()} /></div>);
  fireEvent.click(screen.getByRole('menuitem', { name: 'Ask rows' }));
  expect(close).toHaveBeenCalledOnce();
  expect(action).toHaveBeenCalledWith('map.ask');
});
