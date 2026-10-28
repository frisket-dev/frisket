// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { ExplainBlock } from '../../src/components/rowInspection/CellRunDetails';
import { installPopoverPolyfill } from '../support/domPolyfills';

const { getRunTraceRow } = vi.hoisted(() => ({ getRunTraceRow: vi.fn() }));
vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi: { getRunTraceRow } }),
}));
beforeAll(installPopoverPolyfill);
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it.each(['research.answer', 'research.extract', 'research.web_search'])('offers research activity for %s even without a captured trace', async (actionKind) => {
  getRunTraceRow.mockResolvedValue({ runId: '7', rowId: '1', columnId: '4',
    status: 'not_recorded', recorded: false, trace: null,
    run: { actionKind, actionName: 'Research', status: 'completed', model: 'test/model',
      startedAt: null, finishedAt: null, costActual: null },
  });
  render(<ExplainBlock runId="7" rowId="1" columnId="4" />);
  fireEvent.click(screen.getByTestId('explain-cell'));
  expect(await screen.findByRole('button', { name: 'Research details' })).toBeVisible();
  expect(getRunTraceRow).toHaveBeenCalledWith('7', '1', '4');
});
