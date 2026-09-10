// Parse a composed semantic-search string into structured weighted terms (steer) +
// exclude terms (hard NOT). This is a PARSER over the QuerySpec — the
// backend stores the structured `terms`/`exclude`, never this raw string.
//
// Grammar (text-first, CLI-native):
//   drone            -> term { text: "drone", weight: +1 }   (bare = +1)
//   +drone / -drone  -> +1 / -1            ++drone / --drone -> +2 / -2 (LINEAR)
//   (defense:-0.4)   -> explicit signed weight via parens
//   -(defense:0.4)   -> outer sign applies to the magnitude -> -0.4
//   !defense         -> EXCLUDE term (hard NOT, removes rows about defense)
//   !(defense:0.6)   -> exclude with an explicit cosine-score THRESHOLD (0.6)
//   "crop spraying" / (crop spraying) -> group a multi-word term (quotes/parens)
// `-term` STEERS away (a lean); `!term` EXCLUDES (a gate). Empty tokens are skipped.

export interface ComposedTerm {
  text: string;
  weight: number;
}

export interface ExcludeTerm {
  text: string;
  threshold?: number;
}

export interface ComposedQuery {
  terms: ComposedTerm[];
  exclude: ExcludeTerm[];
}

/** Split on whitespace but keep "quoted"/(paren) groups attached to their sign run
 *  (one of +, -, !). */
function tokenize(raw: string): string[] {
  const s = raw.trim();
  const tokens: string[] = [];
  let i = 0;
  while (i < s.length) {
    while (i < s.length && /\s/.test(s[i])) i++;
    if (i >= s.length) break;
    const start = i;
    while (i < s.length && (s[i] === '+' || s[i] === '-' || s[i] === '!')) i++;
    if (s[i] === '"') {
      i++;
      while (i < s.length && s[i] !== '"') i++;
      if (i < s.length) i++;
    } else if (s[i] === '(') {
      let depth = 0;
      do {
        if (s[i] === '(') depth++;
        else if (s[i] === ')') depth--;
        i++;
      } while (i < s.length && depth > 0);
    } else {
      while (i < s.length && !/\s/.test(s[i])) i++;
    }
    tokens.push(s.slice(start, i));
  }
  return tokens;
}

const EXPLICIT_NUMBER = /^(.*):(-?\d*\.?\d+)$/;

/** Extract the grouped text + optional trailing `:number` from a token body
 *  ("quoted", (paren[:n]), or bareword). */
function bodyTextAndNumber(body: string): { text: string; num: number | null } {
  if (body.startsWith('"')) {
    const end = body.lastIndexOf('"');
    return { text: body.slice(1, end > 0 ? end : undefined).trim(), num: null };
  }
  if (body.startsWith('(')) {
    const inner = body.slice(1, body.endsWith(')') ? -1 : undefined);
    const m = inner.match(EXPLICIT_NUMBER);
    if (m) return { text: m[1].trim(), num: Number.parseFloat(m[2]) };
    return { text: inner.trim(), num: null };
  }
  return { text: body.trim(), num: null };
}

/** Parse one whitespace token into a term or an exclude (returns the bucket). */
function parseToken(
  token: string,
): { kind: 'term'; value: ComposedTerm } | { kind: 'exclude'; value: ExcludeTerm } | null {
  let i = 0;
  let signSum = 0;
  let firstSign = '';
  let isExclude = false;
  while (i < token.length && (token[i] === '+' || token[i] === '-' || token[i] === '!')) {
    if (token[i] === '!') isExclude = true;
    else {
      if (firstSign === '') firstSign = token[i];
      signSum += token[i] === '-' ? -1 : 1;
    }
    i++;
  }
  const { text, num } = bodyTextAndNumber(token.slice(i));
  if (!text) return null;
  if (isExclude) {
    // `!(defense:0.6)` -> threshold 0.6 (a cosine-score cutoff in (0,1]); else default.
    const threshold =
      num !== null && Number.isFinite(num) && num > 0 && num <= 1 ? num : undefined;
    return { kind: 'exclude', value: threshold === undefined ? { text } : { text, threshold } };
  }
  let weight: number;
  if (num !== null && Number.isFinite(num)) {
    weight = (firstSign === '-' ? -1 : 1) * num; // explicit magnitude; outer sign flips
  } else {
    weight = signSum === 0 ? 1 : signSum; // bare = +1; ++ = +2; -- = -2
  }
  return { kind: 'term', value: { text, weight } };
}

/** Parse a raw search string into structured terms (steer) + exclude (hard NOT). */
export function parseComposedQuery(raw: string): ComposedQuery {
  const terms: ComposedTerm[] = [];
  const exclude: ExcludeTerm[] = [];
  for (const token of tokenize(raw)) {
    const parsed = parseToken(token);
    if (!parsed) continue;
    if (parsed.kind === 'term') terms.push(parsed.value);
    else exclude.push(parsed.value);
  }
  return { terms, exclude };
}

/** True when the input is a single plain phrase (no operators, no exclude) — i.e. one
 *  unit-weight term whose text is the raw input verbatim. Such queries take the legacy
 *  single-`text` wire; anything composed sends structured terms/exclude. */
export function isPlainPhrase(raw: string, q: ComposedQuery): boolean {
  return (
    q.exclude.length === 0 &&
    q.terms.length === 1 &&
    q.terms[0].weight === 1 &&
    q.terms[0].text === raw.trim()
  );
}
