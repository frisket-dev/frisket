import { describe, expect, it } from 'vitest';
import { formatUsd, formatUsdOrNone } from '../../src/format';

/** The whole point of this formatter: no amount of real spend may render as
 *  "$0.00", and an absent amount may not look like a tiny one. */
describe('formatUsd', () => {
  it('renders cents and above with two decimals', () => {
    expect(formatUsd(12)).toBe('$12.00');
    expect(formatUsd(3.25)).toBe('$3.25');
    expect(formatUsd(0.01)).toBe('$0.01');
    expect(formatUsd(0.999)).toBe('$1.00');
  });

  it('never prints a false zero for sub-cent spend', () => {
    // The observed defect: $0.001755 of real spend rendered $0.00.
    expect(formatUsd(0.001755)).toBe('$0.0018');
    expect(formatUsd(0.0001)).toBe('$0.0001');
    expect(formatUsd(0.00001)).toBe('$0.00001');
    expect(formatUsd(0.000001064)).toBe('$0.000001');
  });

  it('says "smaller than we can show" rather than showing zero', () => {
    expect(formatUsd(1e-12)).toBe('<$0.00000001');
  });

  it('reserves $0.00 for an amount that really is zero', () => {
    expect(formatUsd(0)).toBe('$0.00');
  });

  it('keeps the sign outside the dollar mark', () => {
    expect(formatUsd(-3.25)).toBe('-$3.25');
    expect(formatUsd(-0.001755)).toBe('-$0.0018');
  });

  it('refuses to render a non-number as money', () => {
    expect(formatUsd(Number.NaN)).toBe('—');
    expect(formatUsd(Number.POSITIVE_INFINITY)).toBe('—');
  });

  it('distinguishes an unset amount from a tiny one', () => {
    // A cap of a hundredth of a cent and no cap at all used to both read
    // "$0.00" / "-" in the AI Providers table.
    expect(formatUsdOrNone(null)).toBe('—');
    expect(formatUsdOrNone(undefined)).toBe('—');
    expect(formatUsdOrNone(0.0001)).toBe('$0.0001');
    expect(formatUsdOrNone(0)).toBe('$0.00');
  });
});
