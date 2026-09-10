// @vitest-environment jsdom
//
// The CostGateModal renders the
// server-computed claim lines (trust-label copy) when the 402's estimate
// carries them, and stays byte-identical to the pre-claims modal when it
// does not (old servers). One deliberate click re-POSTs the exact
// server-issued confirmation token; that echo remains the jobStore/api
// layer's job, not the modal's.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CostGateModal } from '../../src/components/CostGateModal';
import type { RunEstimate } from '../../src/api/open';



afterEach(cleanup);

const CLAIMED_ESTIMATE: RunEstimate = {
  cost: 0,
  rows: 3,
  billed_cost: 0,
  policy_id: 'frisket.identity.v1',
  claims: [
    {
      field: 'egress_class',
      display:
        'Media leaves this machine for your own infrastructure ' +
        '(LAN service you operate). (billed to your own account; no platform charge)',
    },
    {
      field: 'cost',
      display:
        'Estimated provider cost $1.50; this pre-run estimate is not a spending cap ' +
        '(billed to your own account; no platform charge).',
    },
  ],
  promise_set_hash: 'abc123',
};

describe('CostGateModal claims rendering', () => {
  it('renders one line per claim when the estimate carries claims', () => {
    render(
      <CostGateModal
        estimate={CLAIMED_ESTIMATE}
        message="this run needs confirmation"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const list = screen.getByTestId('cost-gate-claims');
    expect(list).toBeInTheDocument();
    expect(screen.getByTestId('cost-gate-claim-egress_class')).toHaveTextContent(
      'Media leaves this machine',
    );
    expect(screen.getByTestId('cost-gate-claim-cost')).toHaveTextContent(
      'Estimated provider cost $1.50; this pre-run estimate is not a spending cap',
    );
  });

  it('renders no claims block when a rated estimate carries no claims', () => {
    render(
      <CostGateModal
        estimate={{
          cost: 2.5,
          rows: 10,
          billed_cost: 2_500_000,
          policy_id: 'frisket.identity.v1',
        }}
        message="estimated cost $2.50 exceeds the $1.00 gate"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('cost-gate-claims')).not.toBeInTheDocument();
    expect(screen.getByTestId('cost-gate-estimate')).toHaveTextContent('$2.50');
  });

  it('claims-only 402 with cost=0 renders claims and confirms with one click', async () => {
    // Confirm-area 2 (R3a critique round): an egress-only gate on a free
    // run arrives as cost: 0 (a number, NOT null/undefined). The modal must
    // treat 0 as a rendered price ($0.00, never UNKNOWN), render the claim
    // lines, and leave confirmation available — a falsy cost is a price, not
    // an absent one.
    const onConfirm = vi.fn();
    render(
      <CostGateModal
        estimate={{
          cost: 0,
          rows: 1,
          billed_cost: 0,
          policy_id: 'frisket.identity.v1',
          claims: [
            {
              field: 'egress_class',
              display: 'Media leaves this machine for your own infrastructure.',
            },
          ],
          promise_set_hash: 'hash-zero-cost',
        }}
        message="this run needs confirmation"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByTestId('cost-gate-claim-egress_class')).toBeInTheDocument();
    expect(screen.getByTestId('cost-gate-estimate')).toHaveTextContent('$0.00');
    expect(screen.getByTestId('cost-gate-estimate')).not.toHaveTextContent('UNKNOWN');
    const confirmButton = screen.getByTestId('cost-gate-confirm');
    expect(confirmButton).toBeEnabled();
    await userEvent.click(confirmButton);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('quotes the BILLED figure when the server rated one', () => {
    // The wave's user-visible point. A hosted deployment rates cost-plus:
    // the provider costs $0.11, the journalist is billed $0.21. The modal is
    // the last thing shown before they agree, so it must quote the figure
    // they will actually pay — and the server hashed that same figure into
    // the consent the confirm echoes back.
    render(
      <CostGateModal
        estimate={{ cost: 0.11, rows: 4, billed_cost: 210_000, policy_id: 'x.v1' }}
        message="estimated cost $0.21 exceeds the $0.10 gate"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const line = screen.getByTestId('cost-gate-estimate');
    expect(line).toHaveTextContent('$0.21');
    expect(line).not.toHaveTextContent('$0.11');
  });

  it('renders UNKNOWN when NO policy rated the run', () => {
    // Every current producer rates before the modal. A missing verdict is an
    // old/malformed envelope; rendering its provider cost would present a
    // figure with no billed-rate authority.
    render(
      <CostGateModal
        estimate={{ cost: 2.5, rows: 10 }}
        message="estimated cost $2.50 exceeds the $1.00 gate"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const line = screen.getByTestId('cost-gate-estimate');
    expect(line).toHaveTextContent('UNKNOWN');
    expect(line).not.toHaveTextContent('$2.50');
  });

  it('a policy that DECLINED to price the run renders UNKNOWN, not the provider cost', () => {
    // The fence for the seam's worst display bug. `billed_cost: null` beside
    // a `policy_id` is the Unpriceable verdict — the deployment said it
    // cannot bill from this number. Rendering $2.50 there would show the user
    // a price the deployment explicitly refused to charge from, and (with the
    // matching server bug) admit the run as cheap. It must read UNKNOWN.
    render(
      <CostGateModal
        estimate={{ cost: 2.5, rows: 10, billed_cost: null, policy_id: 'acme.v1' }}
        message="Estimated cost is unknown (this run has no published price)"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const line = screen.getByTestId('cost-gate-estimate');
    expect(line).toHaveTextContent('UNKNOWN');
    expect(line).not.toHaveTextContent('$2.50');
  });

  it('renders UNKNOWN when neither figure exists', () => {
    render(
      <CostGateModal
        estimate={{ cost: null, rows: 3, billed_cost: null, policy_id: 'x.v1' }}
        message="Estimated cost is unknown"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByTestId('cost-gate-estimate')).toHaveTextContent('UNKNOWN');
  });

  it('a zero billed figure renders as free, not as UNKNOWN', () => {
    // 0 is a price. The `typeof === number` check exists so a free-local run
    // rated at zero micros does not fall through to `cost` (or to UNKNOWN).
    render(
      <CostGateModal
        estimate={{ cost: 0, rows: 3, billed_cost: 0, policy_id: 'x.v1' }}
        message="this run needs confirmation"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const line = screen.getByTestId('cost-gate-estimate');
    expect(line).toHaveTextContent('$0.00');
    expect(line).not.toHaveTextContent('UNKNOWN');
  });

  it('confirms with one click and never renders a typed confirmation control', async () => {
    const onConfirm = vi.fn();
    render(
      <CostGateModal
        estimate={CLAIMED_ESTIMATE}
        message="this run needs confirmation"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    const confirmButton = screen.getByTestId('cost-gate-confirm');
    expect(confirmButton).toBeEnabled();
    expect(screen.queryByTestId('cost-gate-input')).not.toBeInTheDocument();
    await userEvent.click(confirmButton);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
