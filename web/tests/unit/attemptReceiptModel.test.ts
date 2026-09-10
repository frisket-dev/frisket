// Classifying `settle()`'s four answers. The whole point is that three of
// them are not numbers, and that nothing here promotes an absence into a
// zero: `totalUsd` is null when nothing was rated, never 0.

import { describe, expect, it } from 'vitest';

import {
  attemptReceiptsSummary,
  billedQuantityLabel,
  noChargeReason,
  quantityEvidenceLabel,
  rateLabel,
  receiptCharge,
} from '../../src/attemptReceiptModel';
import type { AttemptReceipt, AttemptSettlement } from '../../src/api/open';



function receipt(
  settlement: AttemptSettlement | null,
  runId: number | null = 1,
  borneBy: AttemptReceipt['borne_by'] = { credentialed_providers: [] },
): AttemptReceipt {
  return {
    attempt_id: `attempt_${Math.random()}`,
    run_id: runId,
    seq: 1,
    state: 'effected',
    action_identity_hash: 'h',
    scope: [1],
    target: null,
    consent: null,
    cost_basis: null,
    price_card_version: settlement?.price_card_version ?? null,
    settlement,
    borne_by: borneBy,
    created_at: '2026-07-26T10:00:00+00:00',
  };
}

const FREE: AttemptSettlement = {
  price_card_version: null,
  pricing_key: null,
  charge_usd: '0',
  rated_calls: 0,
  unmetered_calls: 0,
};

const PAID: AttemptSettlement = {
  price_card_version: 'test.synthetic.terms.v1',
  pricing_key: 'test.synthetic.audio_minute',
  unit_rate: '0.017',
  quantity_unit: 'audio_minute',
  metered_quantity: '600',
  metered_unit: 'audio_seconds',
  billable_quantity: '10',
  charge_usd: '0.3',
  rated_calls: 1,
  unmetered_calls: 0,
};

describe('receiptCharge', () => {
  it('reads operator-borne zero as free, not as a $0 charge', () => {
    expect(receiptCharge(receipt(FREE))).toEqual({ kind: 'free' });
  });

  it('reads a rated settlement as a charge', () => {
    expect(receiptCharge(receipt(PAID))).toEqual({
      kind: 'charged',
      amountUsd: 0.3,
      raw: '0.3',
    });
  });

  it('keeps a rated ZERO a charge — the server computed it', () => {
    expect(receiptCharge(receipt({ ...PAID, charge_usd: '0', billable_quantity: '0' })))
      .toEqual({ kind: 'charged', amountUsd: 0, raw: '0' });
  });

  it('reads an unpriceable basis as unknown, never as zero', () => {
    expect(
      receiptCharge(
        receipt({
          price_card_version: 'test.synthetic.terms.v1',
          pricing_key: null,
          charge_usd: null,
        }),
      ),
    ).toEqual({ kind: 'unpriced' });
  });

  it('carries the server’s unsettleable reason through untranslated', () => {
    for (const reason of ['metering_reclaimed', 'unknown_price_card', 'unmetered']) {
      expect(
        receiptCharge(
          receipt({
            price_card_version: 'test.synthetic.terms.v1',
            charge_usd: null,
            unsettleable: reason,
          }),
        ),
      ).toEqual({ kind: 'unavailable', reason });
    }
  });

  it('reads a receipt with no settlement as never-admitted', () => {
    expect(receiptCharge(receipt(null))).toEqual({ kind: 'not_settled' });
  });
});

describe('attemptReceiptsSummary', () => {
  it('reports no total at all when nothing was rated', () => {
    const summary = attemptReceiptsSummary([receipt(FREE), receipt(null)]);
    expect(summary.chargedCount).toBe(0);
    expect(summary.totalUsd).toBeNull();
  });

  it('sums only the rated charges and counts the unavailable ones', () => {
    const summary = attemptReceiptsSummary([
      receipt(PAID),
      receipt({ ...PAID, charge_usd: '0.06' }),
      receipt(FREE),
      receipt({
        price_card_version: 'test.synthetic.terms.v1',
        charge_usd: null,
        unsettleable: 'metering_reclaimed',
      }, null),
    ]);
    expect(summary.chargedCount).toBe(2);
    expect(summary.totalUsd).toBeCloseTo(0.36, 10);
    expect(summary.unavailableCount).toBe(1);
    expect(summary.orphanCount).toBe(1);
  });
});

describe('labels', () => {
  it('formats the billed quantity in the unit it was quoted in', () => {
    expect(billedQuantityLabel(PAID)).toBe('10 audio_minute');
    expect(billedQuantityLabel(FREE)).toBeNull();
  });

  it('formats the pinned rate, and omits it when there is none', () => {
    expect(rateLabel(PAID)).toBe('$0.017 / audio_minute');
    expect(rateLabel(FREE)).toBeNull();
  });

  it('requires an explicit zero before describing meter evidence as complete', () => {
    const legacyPaid = { ...PAID };
    delete legacyPaid.unmetered_calls;

    expect(quantityEvidenceLabel('600 audio_seconds', 0)).toBe('600 audio_seconds');
    expect(quantityEvidenceLabel('600 audio_seconds', 1)).toBe(
      'at least 600 audio_seconds',
    );
    expect(quantityEvidenceLabel('600 audio_seconds', undefined)).toBe(
      'at least 600 audio_seconds (meter completeness unknown)',
    );
    expect(billedQuantityLabel(legacyPaid)).toBe(
      'at least 10 audio_minute (meter completeness unknown)',
    );
  });
});

describe('noChargeReason', () => {
  it('names the user\u2019s own key rather than claiming the run was free', () => {
    const summary = attemptReceiptsSummary([
      receipt(FREE, 1, { credentialed_providers: ['openai'] }),
    ]);
    expect(summary.credentialedProviders).toEqual(['openai']);
    expect(noChargeReason(summary, true)).toBe(
      'No platform charge \u2014 this ran on your own OpenAI key, and that account was billed.',
    );
  });

  it('lists every key that was billed when a run touched two providers', () => {
    const summary = attemptReceiptsSummary([
      receipt(FREE, 1, { credentialed_providers: ['openai'] }),
      receipt(FREE, 1, { credentialed_providers: ['anthropic'] }),
    ]);
    expect(noChargeReason(summary, true)).toContain('Anthropic and OpenAI key');
  });

  it('says free only when every call really did run locally', () => {
    const summary = attemptReceiptsSummary([receipt(FREE)]);
    expect(noChargeReason(summary, true)).toBe(
      'No charges \u2014 this ran locally, at no cost.',
    );
  });

  it('declines to claim free when no metering survives to say', () => {
    const summary = attemptReceiptsSummary([receipt(FREE, 1, null)]);
    expect(summary.bornByKnown).toBe(false);
    expect(noChargeReason(summary, true)).toBe('No charges rated for this run.');
  });

  it('keeps "no attempts" distinct from every cost sentence', () => {
    expect(noChargeReason(attemptReceiptsSummary([]), false)).toBe(
      'No execution attempts recorded.',
    );
  });
});
