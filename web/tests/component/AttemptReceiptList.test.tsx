// @vitest-environment jsdom
//
// The settlement receipt's UI seat. What these pin is honesty, not layout:
//
// - a free/local run gains NO money row (the server answered with an absence,
//   and `settle` refuses to fabricate a $0.00 total; the panel must too);
// - a rated charge renders its amount, quantity + unit, rate and any offering
//   terms version it settled under;
// - a charge the server refused to compute (reclaimed metering, unknown terms)
//   renders the reason, never a number;
// - a compaction-orphaned receipt says its run is gone rather than showing a
//   blank run id — it is reachable at all only from the project-level list.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: { ...actual.api, listAttemptReceipts: vi.fn() },
  };
});

import { AttemptReceiptList } from '../../src/components/AttemptReceiptList';

import type { AttemptReceipt, AttemptReceiptsPage } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const listAttemptReceipts = vi.spyOn(api, 'listAttemptReceipts');
const mockApi = { listAttemptReceipts };
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function receipt(overrides: Partial<AttemptReceipt> = {}): AttemptReceipt {
  return {
    attempt_id: 'attempt_01',
    run_id: 7,
    seq: 1,
    state: 'effected',
    action_identity_hash: 'hash',
    scope: [],
    target: null,
    consent: null,
    cost_basis: { kind: 'operator_borne_zero' },
    price_card_version: null,
    settlement: {
      price_card_version: null,
      pricing_key: null,
      charge_usd: '0',
      rated_calls: 0,
      unmetered_calls: 0,
    },
    borne_by: { credentialed_providers: [] },
    created_at: '2026-07-26T10:00:00+00:00',
    ...overrides,
  };
}

function page(attempts: AttemptReceipt[]): AttemptReceiptsPage {
  return {
    schema_version: 'frisket.attempt_receipts.v1',
    order: 'created_at DESC',
    offset: 0,
    limit: 25,
    total: attempts.length,
    has_more: false,
    next_offset: null,
    run_id: null,
    attempts,
  };
}

const PAID = receipt({
  attempt_id: 'attempt_paid',
  cost_basis: { kind: 'priced' },
  settlement: {
    price_card_version: 'test.synthetic.terms.v1',
    pricing_key: 'test.synthetic.audio_minute',
    unit_rate: '0.017',
    quantity_unit: 'audio_minute',
    metered_quantity: '600',
    metered_unit: 'audio_seconds',
    billable_quantity: '10',
    rated_charge_usd: '0.3',
    charged_quantity: '10',
    charge_usd: '0.3',
    absorbed_overage_usd: '0',
    rated_calls: 2,
    unmetered_calls: 0,
    consented_quantity: '10',
    exceeds_consented: false,
  },
});

describe('AttemptReceiptList', () => {
  it('shows an honest free state with no money row for an operator-borne run', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(page([receipt()]));
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-none')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('attempt-receipt-none')).toHaveTextContent(
      'No charges — this ran locally, at no cost.',
    );
    expect(screen.queryByTestId('attempt-receipt-total')).not.toBeInTheDocument();
    expect(screen.queryByTestId('attempt-receipt-list')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\$0\.00/);
  });

  it('distinguishes "no attempts recorded" from "attempts that cost nothing"', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(page([]));
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-none')).toHaveTextContent(
        'No execution attempts recorded.',
      ),
    );
  });

  it('does not call a BYOK run free — it names the key that was billed', async () => {
    // The live-proof defect: cost_basis operator_borne_zero means the PLATFORM
    // charged nothing. This run was billed to the user's own OpenAI account.
    mockApi.listAttemptReceipts.mockResolvedValue(
      page([receipt({ borne_by: { credentialed_providers: ['openai'] } })]),
    );
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-none')).toBeInTheDocument(),
    );
    const none = screen.getByTestId('attempt-receipt-none');
    expect(none).toHaveTextContent('No platform charge');
    expect(none).toHaveTextContent('your own OpenAI key');
    expect(none).not.toHaveTextContent('at no cost');
  });

  it('keeps an operator-borne receipt inspectable below its no-platform-charge summary', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(
      page([
        receipt({
          scope: [11, 12],
          target: {
            target_id: 'target-openai-whisper',
            transport: 'remote_api',
            engine: 'openai/whisper-1',
            operator: 'openai',
            egress_class: 'external_api',
            region: 'us-east-1',
            credential_source: 'project_key',
          },
          consent: {
            id: 'consent_01',
            grant_basis: 'user_confirmation',
            actor: 'local-user',
            granted_at: '2026-07-26T09:59:00+00:00',
            promise_set_hash: 'promise-hash-01',
          },
          borne_by: { credentialed_providers: ['openai'] },
        }),
      ]),
    );
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-none')).toHaveTextContent(
        'No platform charge',
      ),
    );
    const list = screen.getByTestId('attempt-receipt-list');
    expect(list).toHaveTextContent('target-openai-whisper');
    expect(list).toHaveTextContent('openai/whisper-1');
    expect(list).toHaveTextContent('11, 12');
    expect(list).toHaveTextContent('consent_01');
    expect(list).toHaveTextContent('user_confirmation');
    expect(screen.queryByTestId('attempt-receipt-total')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\$0\.00/);
  });

  it('keeps a scope-only operator-borne receipt inspectable', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(
      page([
        receipt({
          scope: [21, 22],
          target: null,
          consent: null,
        }),
      ]),
    );
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-list')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('attempt-receipt-row')).toHaveTextContent('21, 22');
    expect(screen.getByTestId('attempt-receipt-none')).toHaveTextContent(
      'No charges — this ran locally, at no cost.',
    );
  });

  it('will not upgrade "we have no metering" into "it was free"', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(page([receipt({ borne_by: null })]));
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-none')).toHaveTextContent(
        'No charges rated for this run.',
      ),
    );
    expect(screen.getByTestId('attempt-receipt-none')).not.toHaveTextContent(
      'no cost',
    );
  });

  it('renders the charge, quantity, rate and price card of a rated settlement', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(page([PAID]));
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-total')).toBeInTheDocument(),
    );
    const row = screen.getByTestId('attempt-receipt-row');
    expect(row).toHaveTextContent('$0.30');
    expect(row).toHaveTextContent('10 audio_minute');
    expect(row).toHaveTextContent('600 audio_seconds');
    expect(row).toHaveTextContent('$0.017 / audio_minute');
    expect(row).toHaveTextContent('test.synthetic.audio_minute');
    expect(row).toHaveTextContent('test.synthetic.terms.v1');
  });

  it('shows actual rated work separately from the capped customer charge', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(
      page([
        receipt({
          ...PAID,
          settlement: {
            ...PAID.settlement!,
            metered_quantity: '72000',
            billable_quantity: '1200',
            rated_charge_usd: '36',
            charged_quantity: '10',
            charge_usd: '0.3',
            absorbed_overage_usd: '35.7',
            exceeds_consented: true,
          },
        }),
      ]),
    );
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-row')).toBeInTheDocument(),
    );
    const row = screen.getByTestId('attempt-receipt-row');
    expect(row).toHaveTextContent('Charged$0.30');
    expect(row).toHaveTextContent('Quantity1200 audio_minute');
    expect(row).toHaveTextContent('Rated value$36.00');
    expect(row).toHaveTextContent('Charged quantity10 audio_minute');
    expect(row).toHaveTextContent('Absorbed overage$35.70');
    expect(row).toHaveTextContent('Consented10 audio_minute · exceeded');
  });

  it('renders a provider-direct charge without inventing an offering terms version', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(
      page([
        receipt({
          ...PAID,
          price_card_version: null,
          settlement: { ...PAID.settlement!, price_card_version: null },
        }),
      ]),
    );
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-row')).toBeInTheDocument(),
    );
    const row = screen.getByTestId('attempt-receipt-row');
    expect(row).toHaveTextContent('Charged$0.30');
    expect(row).not.toHaveTextContent('Terms version');
  });

  it('does not treat a legacy receipt with no completeness count as zero', async () => {
    const legacySettlement = { ...PAID.settlement! };
    delete legacySettlement.unmetered_calls;

    mockApi.listAttemptReceipts.mockResolvedValue(
      page([
        receipt({
          ...PAID,
          attempt_id: 'attempt_legacy',
          settlement: legacySettlement,
        }),
      ]),
    );
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-row')).toBeInTheDocument(),
    );
    const row = screen.getByTestId('attempt-receipt-row');
    expect(row).toHaveTextContent(
      'at least 10 audio_minute (meter completeness unknown)',
    );
    expect(row).toHaveTextContent(
      'at least 600 audio_seconds (meter completeness unknown)',
    );
  });

  it('labels a paged subtotal as partial instead of claiming it is the whole receipt history', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue({
      ...page([PAID]),
      total: 40,
      has_more: true,
      next_offset: 25,
    });
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-total')).toBeInTheDocument(),
    );
    const total = screen.getByTestId('attempt-receipt-total');
    expect(total).toHaveTextContent('shown');
    expect(total).toHaveTextContent('40 receipts total');
  });

  it('reports a charge the server refused to compute as a reason, not a number', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(
      page([
        receipt({
          run_id: null,
          settlement: {
            price_card_version: 'test.synthetic.terms.v1',
            charge_usd: null,
            unsettleable: 'metering_reclaimed',
          },
        }),
      ]),
    );
    render(<AttemptReceiptList />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-row')).toBeInTheDocument(),
    );
    const row = screen.getByTestId('attempt-receipt-row');
    expect(row).toHaveTextContent('not available');
    expect(row).toHaveTextContent('reclaimed by compaction');
    expect(row.textContent).not.toMatch(/\$/);
    // Ruling 7's orphan, named rather than left blank.
    expect(screen.getByTestId('attempt-receipt-orphan')).toHaveTextContent(
      'reclaimed',
    );
  });

  it('does not describe partially invalid metering as no metered calls', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(
      page([
        receipt({
          settlement: {
            price_card_version: 'test.synthetic.terms.v1',
            pricing_key: 'test.synthetic.audio_minute',
            unit_rate: '0.017',
            quantity_unit: 'audio_minute',
            metered_quantity: '60',
            metered_unit: 'audio_seconds',
            billable_quantity: '1',
            charge_usd: null,
            rated_calls: 1,
            unmetered_calls: 1,
            unsettleable: 'unmetered',
          },
        }),
      ]),
    );
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-row')).toBeInTheDocument(),
    );
    const row = screen.getByTestId('attempt-receipt-row');
    expect(row).toHaveTextContent(
      'valid metering is missing for all or part of this priced attempt',
    );
    expect(row).toHaveTextContent('total cannot be rated');
    expect(row).not.toHaveTextContent('no metered calls');
    expect(row).toHaveTextContent('at least 1 audio_minute');
    expect(row).toHaveTextContent('at least 60 audio_seconds');
  });

  it('asks the project-wide reader when no run is given, and the run filter otherwise', async () => {
    mockApi.listAttemptReceipts.mockResolvedValue(page([]));
    const { unmount } = render(<AttemptReceiptList />);
    await waitFor(() => expect(mockApi.listAttemptReceipts).toHaveBeenCalled());
    expect(mockApi.listAttemptReceipts).toHaveBeenCalledWith(null, 25);
    unmount();

    mockApi.listAttemptReceipts.mockClear();
    render(<AttemptReceiptList runId={12} />);
    await waitFor(() => expect(mockApi.listAttemptReceipts).toHaveBeenCalled());
    expect(mockApi.listAttemptReceipts).toHaveBeenCalledWith(12, 25);
  });

  it('surfaces a failed load instead of implying there were no charges', async () => {
    mockApi.listAttemptReceipts.mockRejectedValue(new Error('boom'));
    render(<AttemptReceiptList runId={7} />);

    await waitFor(() =>
      expect(screen.getByTestId('attempt-receipt-error')).toHaveTextContent('boom'),
    );
    expect(screen.queryByTestId('attempt-receipt-none')).not.toBeInTheDocument();
  });
});
