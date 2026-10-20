import { expect, it } from 'vitest';
import { formatBytes, formatNumberDisplay } from '../../src/format';

it.each([
  [0, '0 B'], [1023, '1023 B'], [1024, '1 KB'], [1536, '1.5 KB'],
  [1024 ** 2, '1 MB'], [1024 ** 3, '1 GB'], [1024 ** 4, '1 TB'],
  [null, '—'], [undefined, '—'], [-1, '—'], [NaN, '—'], [Infinity, '—'],
])('formats %s consistently across cells and controls', (value, expected) => {
  expect(formatBytes(value)).toBe(expected);
  if (typeof value === 'number') expect(formatNumberDisplay(value, 'filesize')).toBe(expected);
});
