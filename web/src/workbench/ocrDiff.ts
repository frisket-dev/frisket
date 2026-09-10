// Symmetric word-level OCR diff with character marks.
//
// There is no ground truth, so a disagreement is never "wrong": both sides get
// ONE amber mark. We diff by WORD, then mark the differing CHARACTERS inside a
// changed word (MER[L]DIAN vs MER[I]DIAN). Whitespace runs are normalized first
// so line-wrap/reflow differences are not counted as token changes.

export interface CharSpan {
  text: string;
  changed: boolean;
}

export interface DiffToken {
  text: string;
  changed: boolean;
  chars: CharSpan[];
}

export interface WordDiff {
  left: DiffToken[];
  right: DiffToken[];
  /** Number of disagreement groups (a replace/insert/delete each count once). */
  changedCount: number;
  /** True when every disagreement is a character-diffable word replacement. */
  allCharLevel: boolean;
}

export function normalizeWhitespace(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

function tokenize(text: string): string[] {
  const normalized = normalizeWhitespace(text);
  return normalized ? normalized.split(' ') : [];
}

type WordOp =
  | { kind: 'equal'; text: string }
  | { kind: 'delete'; text: string }
  | { kind: 'insert'; text: string };

function lcsOps(a: string[], b: string[]): WordOp[] {
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops: WordOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ kind: 'equal', text: a[i] });
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ kind: 'delete', text: a[i] });
      i += 1;
    } else {
      ops.push({ kind: 'insert', text: b[j] });
      j += 1;
    }
  }
  while (i < n) {
    ops.push({ kind: 'delete', text: a[i] });
    i += 1;
  }
  while (j < m) {
    ops.push({ kind: 'insert', text: b[j] });
    j += 1;
  }
  return ops;
}

function charSpans(source: string, other: string): CharSpan[] {
  const a = Array.from(source);
  const b = Array.from(other);
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const flags: boolean[] = new Array(n).fill(true);
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      flags[i] = false;
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      i += 1;
    } else {
      j += 1;
    }
  }
  const spans: CharSpan[] = [];
  for (let k = 0; k < n; k += 1) {
    const changed = flags[k];
    const last = spans[spans.length - 1];
    if (last && last.changed === changed) {
      last.text += a[k];
    } else {
      spans.push({ text: a[k], changed });
    }
  }
  return spans;
}

function plainToken(text: string, changed: boolean): DiffToken {
  return { text, changed, chars: [{ text, changed }] };
}

export function diffWords(leftText: string, rightText: string): WordDiff {
  const ops = lcsOps(tokenize(leftText), tokenize(rightText));
  const left: DiffToken[] = [];
  const right: DiffToken[] = [];
  let changedCount = 0;
  let allCharLevel = true;

  // Pair a run of deletes with the following run of inserts into replacements
  // (positionally); leftovers are pure deletes/inserts.
  let pendingDeletes: string[] = [];
  let pendingInserts: string[] = [];

  const flush = () => {
    const pairs = Math.min(pendingDeletes.length, pendingInserts.length);
    for (let k = 0; k < pairs; k += 1) {
      const a = pendingDeletes[k];
      const b = pendingInserts[k];
      left.push({ text: a, changed: true, chars: charSpans(a, b) });
      right.push({ text: b, changed: true, chars: charSpans(b, a) });
      changedCount += 1;
    }
    for (let k = pairs; k < pendingDeletes.length; k += 1) {
      left.push(plainToken(pendingDeletes[k], true));
      changedCount += 1;
      allCharLevel = false;
    }
    for (let k = pairs; k < pendingInserts.length; k += 1) {
      right.push(plainToken(pendingInserts[k], true));
      changedCount += 1;
      allCharLevel = false;
    }
    pendingDeletes = [];
    pendingInserts = [];
  };

  for (const op of ops) {
    if (op.kind === 'equal') {
      flush();
      left.push(plainToken(op.text, false));
      right.push(plainToken(op.text, false));
    } else if (op.kind === 'delete') {
      pendingDeletes.push(op.text);
    } else {
      pendingInserts.push(op.text);
    }
  }
  flush();

  return { left, right, changedCount, allCharLevel };
}
