// The ONE cost selector, and the wire projection that feeds it.
//
// Every surface that puts a price in front of a user — the action panel's cost
// line, the sheet-info re-run banner, the 402 modal — reads
// `actions/quotedCost`. They must, because the server HASHES what it quotes: a
// panel rendering the provider's $0.11 while the gate hashes a billed $0.21 is
// the two-surfaces defect with money in it, and under the open edition's
// identity policy (where the two numbers are equal) it is invisible.
//
// These tests are the three states in isolation; the surface tests
// (ActionFormTranscribeAudioEstimate, CostGateModal) prove each surface is
// actually wired to them, and `noRawCostRendering` proves none of them has
// quietly grown a second answer.

import { describe, expect, it } from 'vitest';

import { quotedCost, quotedUsd } from '../../src/actions/quotedCost';
import { runEstimateFromV1Wire } from '../../src/api/real';

describe('quotedCost — three states', () => {
  it('a rated quote reads the BILLED figure, in micro-dollars', () => {
    expect(
      quotedCost({ cost: 0.11, rows: 4, billed_cost: 210_000, policy_id: 'acme.v1' }),
    ).toEqual({ usd: 0.21, refused: false });
  });

  it('the open edition bills the provider cost, so the two agree', () => {
    expect(
      quotedCost({ cost: 0.11, rows: 4, billed_cost: 110_000, policy_id: 'identity' }),
    ).toEqual({ usd: 0.11, refused: false });
  });

  it('a DECLINED price is null AND flagged — never the provider cost', () => {
    // The dangerous middle state. A `billed_cost: null` beside a `policy_id`
    // means the deployment said it cannot bill from this run; falling back to
    // `cost` would show a confident figure it has already disowned.
    expect(
      quotedCost({ cost: 0.11, rows: 4, billed_cost: null, policy_id: 'acme.v1' }),
    ).toEqual({ usd: null, refused: true });
  });

  it('no policy verdict refuses the old provider-cost shape', () => {
    // Every current producer rates before a 402. A missing verdict is an old
    // or malformed envelope, never permission to render provider cost as the
    // deployment's billed quote.
    expect(quotedCost({ cost: 0.11, rows: 4 })).toEqual({ usd: null, refused: true });
    expect(quotedCost({ cost: null, rows: 4 })).toEqual({ usd: null, refused: true });
  });

  it('an absent estimate is not a price', () => {
    expect(quotedCost(null)).toEqual({ usd: null, refused: false });
    expect(quotedUsd(undefined)).toBeNull();
  });

  it('zero is a price, not an absence', () => {
    expect(quotedCost({ cost: 0, rows: 1, billed_cost: 0, policy_id: 'x' })).toEqual({
      usd: 0,
      refused: false,
    });
  });

  it('an empty policy_id refuses rather than falling back', () => {
    // Defensive: the server never sends one (the field is required and
    // min_length=1), but falling back would turn a malformed payload's
    // provider figure into a billed quote.
    expect(quotedCost({ cost: 0.11, rows: 1, policy_id: '', billed_cost: null })).toEqual({
      usd: null,
      refused: true,
    });
  });

  it('a malformed billed amount refuses rather than rendering it', () => {
    expect(
      quotedCost({ cost: 0.11, rows: 1, policy_id: 'acme.v1', billed_cost: -1 }),
    ).toEqual({ usd: null, refused: true });
  });
});

describe('the estimate endpoint projection', () => {
  it('carries billed_cost and policy_id through to the panel', () => {
    // `runEstimateFromV1Wire` is a WHITELIST — a field it does not name is
    // dropped before any surface sees it. That is exactly how the panel ended
    // up rendering the provider cost while the modal rendered the billed one.
    const projected = runEstimateFromV1Wire({
      rows: 1,
      cost: 0.11,
      audio_seconds: 10.64,
      billed_cost: 210_000,
      policy_id: 'acme.cost_plus.v1',
      billing_label: 'Metered through Acme credits',
      venue_label: 'Acme shared infrastructure',
      cost_source: 'commercial_offering',
    });

    expect(projected.billed_cost).toBe(210_000);
    expect(projected.policy_id).toBe('acme.cost_plus.v1');
    expect(projected.billing_label).toBe('Metered through Acme credits');
    expect(projected.venue_label).toBe('Acme shared infrastructure');
    expect(projected.cost_source).toBe('commercial_offering');
    // ...and the selector then reads the billed figure off it, which is the
    // whole point of carrying them.
    expect(quotedCost(projected).usd).toBe(0.21);
  });

  it('carries free-public-API provenance without turning it into a price', () => {
    const projected = runEstimateFromV1Wire({
      rows: 4,
      cost: null,
      cost_source: 'free_public_api',
    });

    expect(projected.cost_source).toBe('free_public_api');
    expect(projected.cost).toBeNull();
  });

  it('preserves absence so the selector can refuse an incomplete verdict', () => {
    const projected = runEstimateFromV1Wire({ rows: 1, cost: 0.11 });
    expect(projected.billed_cost).toBeUndefined();
    expect(projected.policy_id).toBeUndefined();
    expect(quotedCost(projected)).toEqual({ usd: null, refused: true });
  });

  it('carries a DECLINED verdict rather than flattening it to absent', () => {
    const projected = runEstimateFromV1Wire({
      rows: 1,
      cost: 0.11,
      billed_cost: null,
      policy_id: 'acme.cost_plus.v1',
    });
    expect(quotedCost(projected)).toEqual({ usd: null, refused: true });
  });
});
