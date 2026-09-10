import { describe, expect, it } from 'vitest';

import {
  API_CALL_MAX_TIMEOUT_SECONDS,
  isValidApiCallRate,
  isValidApiCallTimeout,
} from '../../src/actions/apiCallValidation';

describe('isValidApiCallRate', () => {
  it('accepts an ordinary positive rate', () => {
    expect(isValidApiCallRate(5)).toBe(true);
  });

  it('rejects zero', () => {
    expect(isValidApiCallRate(0)).toBe(false);
  });

  it('rejects negative rates', () => {
    expect(isValidApiCallRate(-1)).toBe(false);
  });

  it('rejects Infinity', () => {
    expect(isValidApiCallRate(Infinity)).toBe(false);
  });

  it('rejects NaN', () => {
    expect(isValidApiCallRate(NaN)).toBe(false);
  });

  it('rejects a rate so small its pacing interval (1/value) overflows to Infinity', () => {
    // 1e-309 is itself finite, but 1 / 1e-309 = 1e309 overflows past
    // Number.MAX_VALUE — the reciprocal check exists specifically to catch this.
    expect(isValidApiCallRate(1e-309)).toBe(false);
  });

  it('accepts a small rate whose reciprocal stays finite', () => {
    expect(isValidApiCallRate(1e-10)).toBe(true);
  });

  it('rejects non-number values', () => {
    expect(isValidApiCallRate('5')).toBe(false);
    expect(isValidApiCallRate(null)).toBe(false);
    expect(isValidApiCallRate(undefined)).toBe(false);
  });
});

describe('isValidApiCallTimeout', () => {
  it('accepts an ordinary positive timeout', () => {
    expect(isValidApiCallTimeout(30)).toBe(true);
  });

  it('accepts the exact server-side ceiling', () => {
    expect(isValidApiCallTimeout(API_CALL_MAX_TIMEOUT_SECONDS)).toBe(true);
  });

  it('rejects a timeout past the server-side ceiling', () => {
    expect(isValidApiCallTimeout(API_CALL_MAX_TIMEOUT_SECONDS + 0.0001)).toBe(false);
  });

  it('rejects zero', () => {
    expect(isValidApiCallTimeout(0)).toBe(false);
  });

  it('rejects negative timeouts', () => {
    expect(isValidApiCallTimeout(-1)).toBe(false);
  });

  it('rejects Infinity', () => {
    expect(isValidApiCallTimeout(Infinity)).toBe(false);
  });

  it('rejects NaN', () => {
    expect(isValidApiCallTimeout(NaN)).toBe(false);
  });

  it('rejects non-number values', () => {
    expect(isValidApiCallTimeout('30')).toBe(false);
    expect(isValidApiCallTimeout(null)).toBe(false);
    expect(isValidApiCallTimeout(undefined)).toBe(false);
  });
});
