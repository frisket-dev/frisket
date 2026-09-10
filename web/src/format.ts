/** Human-readable display helpers shared by the grid and drawers. */

import pluralize from 'pluralize';

const USD_WHOLE_FORMAT = new Intl.NumberFormat(undefined, {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

const USD_PRECISE_FORMAT = new Intl.NumberFormat(undefined, {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
});

const GROUPED_INTEGER_FORMAT = new Intl.NumberFormat(undefined, {
  useGrouping: true,
  maximumFractionDigits: 0,
});

const GROUPED_NUMBER_FORMAT = new Intl.NumberFormat(undefined, {
  useGrouping: true,
  maximumFractionDigits: 20,
});

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return String(n);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u += 1;
  }
  const text = u === 0 ? String(v) : v.toFixed(v < 10 && u > 0 ? 1 : 0).replace(/\.0$/, '');
  return `${text} ${units[u]}`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

export function formatTokens(n: number | null | undefined): string {
  if (n == null) return '—';
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

export function coerceFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value.replace(/,/g, ''));
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

export function formatNumberDisplay(n: number, format?: string | null): string | null {
  if (format === 'filesize') return formatBytes(n);
  if (format === 'currency') {
    return (Math.abs(n) >= 100 ? USD_WHOLE_FORMAT : USD_PRECISE_FORMAT).format(n);
  }
  if (format === 'percent') {
    const pct = n * 100;
    return `${Number.isInteger(pct) ? pct.toFixed(0) : pct.toFixed(1)}%`;
  }
  return null;
}

export function formatDefaultNumberDisplay(n: number, type?: string | null): string {
  if (!Number.isFinite(n)) return String(n);
  const integerLike = type === 'integer' || Number.isInteger(n);
  return (integerLike ? GROUPED_INTEGER_FORMAT : GROUPED_NUMBER_FORMAT).format(n);
}

/** Apply a column's display format to a raw cell value (for grid display). */
export function formatCell(value: unknown, format?: string | null): string | null {
  if (value == null) return null;
  const n = coerceFiniteNumber(value);
  if (n !== null) return formatNumberDisplay(n, format);
  return null; // null = "no special formatting, use default rendering"
}

// ---------------------------------------------------------------------------
// Money.
//
// ONE formatter for every spend/cap/estimate/charge amount in the app. There
// used to be five (an exported `formatUsd`, two private `usd` helpers in
// Settings and Admin, a micro-dollar `formatCost` in Sources, and two inline
// `toFixed` sites), and four of them rounded to two decimals — so the AI
// Providers SPENT column printed $0.00 against $0.001755 of real spend, and a
// $0.0001 cap printed "$0.00", which reads as no cap at all.
//
// The rule: NEVER render a false zero. Two decimals down to a cent, then as
// many decimals as it takes to show the amount's first significant digit.
// $0.00 is reserved for an amount that really is zero, and an absent amount
// renders as an em dash — the two must not look alike.

/** How many decimals a sub-cent amount needs before it stops reading as zero:
 *  enough to reach its first significant digit, at least 4, capped so a
 *  denormal cannot produce a 300-character string. */
const MAX_USD_DECIMALS = 8;

export function formatUsd(n: number): string {
  if (!Number.isFinite(n)) return '—';
  const abs = Math.abs(n);
  const sign = n < 0 ? '-' : '';
  if (abs === 0) return '$0.00';
  if (abs >= 0.01) return `${sign}$${abs.toFixed(2)}`;
  const decimals = Math.min(
    Math.max(4, -Math.floor(Math.log10(abs))),
    MAX_USD_DECIMALS,
  );
  const text = abs.toFixed(decimals);
  // Smaller than the widest rendering can show. Still not zero, and saying so
  // beats printing one.
  if (Number(text) === 0) return `${sign}<$${(10 ** -MAX_USD_DECIMALS).toFixed(MAX_USD_DECIMALS)}`;
  return `${sign}$${text}`;
}

/** Money that may be absent. An em dash, never "$0.00": "no cap set" and "a
 *  cap of a hundredth of a cent" are different facts and must look different. */
export function formatUsdOrNone(n: number | null | undefined): string {
  return n == null ? '—' : formatUsd(n);
}

/** Confidence → CSS class for the colored pill (shared by RowDrawer + ReviewQueue). */
export function confClass(c: number): string {
  return c >= 0.8 ? 'conf-high' : c >= 0.55 ? 'conf-mid' : 'conf-low';
}

// ---------------------------------------------------------------------------
// Count-noun inflection (shared by the grid's JSON-array count badges and
// child-sheet count chips — a column/sheet literally named "people" naively
// pluralized to "2 peoples").
//
// Delegates to the `pluralize` library (irregular-aware, case-preserving,
// idempotent — safe to call `.singular()`/`.plural()` regardless of which
// form the input is already in) instead of a hand-rolled inflection engine.
// The overrides below are the ones actually needed: every word here was
// checked against the library's *default* output and found to differ from
// what this app's count badges need — either a genuine mistake (the library
// mis-singularizes "cookies"/"caches"/"lens"/"bias"/… by guessing a stem that
// isn't a word) or a deliberate app-level call the library doesn't make on
// its own (declining to resolve an ambiguous "-oes" plural like "heroes" back
// to "hero", because "1 heroe" reads as a typo). See countLabel.test.ts for
// the cases these exist to satisfy.
const IRREGULAR_PAIRS: [singular: string, plural: string][] = [
  // "-ie" nouns the library's default "-ies"→"-y" rule mis-singularizes
  // ("cookies"→"cooky", "caches"→"cach", "calories"→"calory",
  // "selfies"→"selfy"). Note "movie"/"zombie" are NOT here — the library
  // already gets those right by default.
  ['cookie', 'cookies'],
  ['cache', 'caches'],
  ['calorie', 'calories'],
  ['selfie', 'selfies'],
  // Singular nouns ending in "s" that the library's default rules mistake
  // for a plural and mis-singularize by stripping a letter ("lens"→"len",
  // "bias"→"bia", "canvas"→"canva", "summons"→"summon", "chaos"→"chao",
  // "cosmos"→"cosmo", "ethos"→"etho", "pathos"→"patho"). Note "gas"/"bus"/
  // "atlas"/"alias" are NOT here — the library already gets those right.
  ['lens', 'lenses'],
  ['bias', 'biases'],
  ['canvas', 'canvases'],
  ['summons', 'summonses'],
  ['chaos', 'chaoses'],
  ['cosmos', 'cosmoses'],
  ['ethos', 'ethoses'],
  ['pathos', 'pathoses'],
];

// Words treated as invariant (same spelling at any count). Two different
// reasons land a word here:
//  - genuinely uncountable nouns the library's default rules don't already
//    treat as invariant ("data"→"datum", "metadata"→"metadatum",
//    "metrics"→"metric", "means"→"mean", "biceps"→"bicep" are all real
//    dictionary singulars the library produces on request — not what a count
//    badge for these column names should show). Note "series"/"species"/
//    "news"/"media"/"analytics"/"corps"/"kudos" are NOT here — the library
//    already treats those as invariant by default.
//  - "-oes" plurals with no unambiguous singular reading ("heroes"/"shoes"/
//    "tomatoes"): the library WOULD confidently resolve these to
//    "hero"/"shoe"/"tomato" (those are the correct dictionary singulars), but
//    this app's count badges intentionally decline that resolution rather
//    than guess which of two equally-plausible stems ("hero"+"es" vs a stem
//    ending in "-o") a user-typed column name meant.
const INVARIANT_WORDS = [
  'data', 'metadata', 'metrics', 'means', 'biceps',
  'heroes', 'shoes', 'tomatoes',
];

for (const [singular, plural] of IRREGULAR_PAIRS) pluralize.addIrregularRule(singular, plural);
for (const word of INVARIANT_WORDS) pluralize.addUncountableRule(word);

/** Pluralize/singularize `noun` for count `n` (people↔person, entity↔entities,
 *  row↔rows, …), backed by the `pluralize` library plus the domain overrides
 *  above. Preserves the leading letter's case and is safe to call on a noun
 *  already in the target form (idempotent in both directions). */
export function pluralizeNoun(noun: string, n: number): string {
  const trimmed = noun.trim();
  if (!trimmed) return trimmed;
  return n === 1 ? pluralize.singular(trimmed) : pluralize.plural(trimmed);
}

/** "3 people" / "1 person" / "1,204 rows" — a full "<n> <noun>" count label
 *  built on pluralizeNoun, with the count grouped for readability.
 *  `fallback` names the noun when `noun` is blank (e.g. an unnamed
 *  column/sheet). */
export function countLabel(n: number, noun: string, fallback = 'items'): string {
  const base = noun.trim() || fallback;
  return `${n.toLocaleString()} ${pluralizeNoun(base, n)}`;
}

const RELATIVE_TIME_FORMAT = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
const RELATIVE_TIME_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 60 * 60 * 24 * 365],
  ['month', 60 * 60 * 24 * 30],
  ['week', 60 * 60 * 24 * 7],
  ['day', 60 * 60 * 24],
  ['hour', 60 * 60],
  ['minute', 60],
];

/** "3 hours ago" / "just now" from an ISO timestamp (e.g. Home screen cards'
 *  last-modified). Returns null for missing/unparseable input. */
export function formatRelativeTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return null;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return 'just now';
  for (const [unit, unitSeconds] of RELATIVE_TIME_UNITS) {
    if (seconds >= unitSeconds) {
      return RELATIVE_TIME_FORMAT.format(-Math.round(seconds / unitSeconds), unit);
    }
  }
  return RELATIVE_TIME_FORMAT.format(-Math.round(seconds / 60), 'minute');
}
