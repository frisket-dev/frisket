/** Match full JSON values. Only null and '' are unchecked; whitespace is a value.
 * Sets accept arrays or JSON-encoded arrays. Slash dates are US month/day/year;
 * two-digit years mean 2000–2099. */
export type MatchVerdict = 'match' | 'miss' | 'unchecked' | 'unscored' | 'not_run';

type Options = { state?: string; handVerdict?: string };

function number(value: unknown): string | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  if (typeof value === 'number' && !Number.isFinite(value)) return null;
  const match = String(value).trim().match(/^([+-]?)(?:\$)?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE]([+-]?\d+))?$/);
  if (!match) return null;
  const [whole, fraction = ''] = match[2].replaceAll(',', '').split('.');
  let coefficient = (whole + fraction).replace(/^0+/, '');
  if (!coefficient) return '0';
  const trailing = coefficient.length - coefficient.replace(/0+$/, '').length;
  coefficient = coefficient.replace(/0+$/, '');
  return `${match[1] === '-' ? '-' : ''}${coefficient}e${Number(match[3] ?? 0) - fraction.length + trailing}`;
}

function canonical(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'number') return `number:${number(value)}`;
  if (typeof value === 'string') return `string:${JSON.stringify(value)}`;
  if (typeof value === 'boolean') return `bool:${value}`;
  if (Array.isArray(value)) return `array:[${value.map(canonical).join(',')}]`;
  if (typeof value === 'object') return `object:{${Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`;
  return 'unsupported';
}

const months = ['jan january', 'feb february', 'mar march', 'apr april', 'may', 'jun june', 'jul july', 'aug august', 'sep sept september', 'oct october', 'nov november', 'dec december'];
function date(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  const iso = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const slash = text.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})$/);
  const named = text.match(/^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$/);
  let y: number, m: number, d: number;
  if (iso) [y, m, d] = iso.slice(1).map(Number);
  else if (slash) { m = Number(slash[1]); d = Number(slash[2]); y = Number(slash[3]) + (slash[3].length === 2 ? 2000 : 0); }
  else if (named) { m = months.findIndex(names => names.split(' ').includes(named[1].toLowerCase())) + 1; d = Number(named[2]); y = Number(named[3]); }
  else return null;
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (y < 1 || m < 1 || m > 12 || d < 1 || d > days[m - 1]) return null;
  return `${y}-${m}-${d}`;
}

function items(value: unknown): Set<string> | null {
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return null; }
  }
  return Array.isArray(value) ? new Set(value.map(canonical)) : null;
}

export function matchAnswer(rule: string, expected: unknown, answer: unknown, options: Options = {}): MatchVerdict {
  if (['missing', 'pending', 'cancelled'].includes(options.state ?? 'value')) return 'not_run';
  if (expected === null || expected === '') return 'unchecked';
  if (options.state === 'error') return 'miss';
  if (rule === 'hand') return options.handVerdict === 'match' || options.handVerdict === 'miss' ? options.handVerdict : 'unscored';
  if (expected === '[empty]') return answer === null || (typeof answer === 'string' && !answer.trim()) || (Array.isArray(answer) && !answer.length) ? 'match' : 'miss';
  let matches: boolean;
  if (rule === 'exact') matches = canonical(expected) === canonical(answer);
  else if (rule === 'ignore_case') matches = typeof expected === 'string' && typeof answer === 'string' && expected.trim().toLowerCase() === answer.trim().toLowerCase();
  else if (rule === 'number' || rule === 'date') {
    const parse = rule === 'number' ? number : date;
    const a = parse(expected), b = parse(answer);
    matches = a !== null && b !== null && a === b;
  } else if (rule === 'set') {
    const a = items(expected), b = items(answer);
    matches = a !== null && b !== null && a.size === b.size && [...a].every(item => b.has(item));
  } else throw new Error(`Unknown match rule: ${rule}`);
  return matches ? 'match' : 'miss';
}
