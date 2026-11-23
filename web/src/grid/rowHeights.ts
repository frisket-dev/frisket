/** Grid row geometry. Rows are one line tall; with wrap on, a row grows to fit
 *  its longest cell but never past WRAP_MAX_LINES, so one huge cell cannot take
 *  over the grid. Cells clip at that height. */
export const ROW_HEIGHT = 36;
export const WRAP_MAX_LINES = 4;
export const WRAP_LINE_HEIGHT = 18;
export const WRAP_PADDING_Y = 9;
export const WRAP_ROW_MAX_HEIGHT = WRAP_PADDING_Y * 2 + WRAP_MAX_LINES * WRAP_LINE_HEIGHT;

/** Greedy word wrap of `text` into at most `maxLines` lines no wider than
 *  `width`; the last kept line ends in an ellipsis when text remains. */
export function wrapLines(
  text: string,
  width: number,
  measure: (value: string) => number,
  maxLines = WRAP_MAX_LINES,
): string[] {
  const lines: string[] = [];
  const paragraphs = text.split(/\r?\n/);
  let truncated = false;
  for (let p = 0; p < paragraphs.length; p += 1) {
    const words = paragraphs[p].split(/(\s+)/).filter((part) => part !== '');
    let line = '';
    for (const word of words) {
      const candidate = line + word;
      if (line !== '' && measure(candidate.trimEnd()) > width) {
        lines.push(line.trimEnd());
        line = word.trimStart();
      } else {
        line = candidate;
      }
      while (measure(line) > width && line.length > 1) {
        let cut = line.length - 1;
        while (cut > 1 && measure(line.slice(0, cut)) > width) cut -= 1;
        lines.push(line.slice(0, cut));
        line = line.slice(cut);
      }
      if (lines.length >= maxLines) break;
    }
    if (lines.length >= maxLines) {
      truncated = true;
      break;
    }
    lines.push(line.trimEnd());
    if (lines.length >= maxLines && p < paragraphs.length - 1) {
      truncated = true;
      break;
    }
  }
  const kept = lines.slice(0, maxLines);
  if (truncated || lines.length > maxLines) {
    let last = kept[kept.length - 1] ?? '';
    while (last.length > 0 && measure(`${last}…`) > width) last = last.slice(0, -1);
    kept[kept.length - 1] = `${last}…`;
  }
  return kept;
}

/** Height of a wrapped row whose tallest cell needs `lines` lines. */
export function wrappedRowHeight(lines: number): number {
  const clamped = Math.max(1, Math.min(WRAP_MAX_LINES, lines));
  return Math.max(ROW_HEIGHT, WRAP_PADDING_Y * 2 + clamped * WRAP_LINE_HEIGHT);
}
