import { GridCellKind, type DrawCellCallback } from '@glideapps/glide-data-grid';
import type { ColumnDef, Row } from '../api/types';
import type { RowFilterTints } from './gridTheme';
import { ROW_HEIGHT, WRAP_LINE_HEIGHT, WRAP_PADDING_Y, wrapLines, wrappedRowHeight } from './rowHeights';

type Measure = (text: string) => number;

/** Canvas text width in `font`; without a canvas (tests), an average glyph width. */
export function textMeasure(font: string): Measure {
  const context = typeof document === 'undefined' ? null : document.createElement('canvas').getContext('2d');
  if (!context) return (text) => text.length * 7;
  context.font = font;
  return (text) => context.measureText(text).width;
}

/** Every case-insensitive occurrence of `needle` in `line`, as [start, end). */
export function matchRanges(line: string, needle: string): Array<[number, number]> {
  const query = needle.trim().toLocaleLowerCase();
  if (!query) return [];
  const haystack = line.toLocaleLowerCase();
  const ranges: Array<[number, number]> = [];
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(query, from);
    if (at < 0) return ranges;
    ranges.push([at, at + query.length]);
    from = at + query.length;
  }
}

function singleLine(text: string, width: number, measure: Measure): string {
  const line = text.split(/\r?\n/, 1)[0] ?? '';
  if (measure(line) <= width) return line;
  let cut = line.length;
  while (cut > 0 && measure(`${line.slice(0, cut)}…`) > width) cut -= 1;
  return `${line.slice(0, cut)}…`;
}

interface RowFilterDrawOptions {
  wrap: boolean;
  /** Search text and the columns it covers; null when no search is active. */
  search: { text: string; covers(column: number): boolean } | null;
  tints: RowFilterTints;
}

/** Text cells draw their own lines when wrapping (top-aligned, clamped to the
 *  row's maximum) or when a search covers the column (matches highlighted).
 *  Every other cell keeps the grid's own renderer. */
export function rowFilterDrawCell({ wrap, search, tints }: RowFilterDrawOptions): DrawCellCallback | undefined {
  if (!wrap && !search) return undefined;
  return (args, drawContent) => {
    const { ctx, cell, theme, rect, col } = args;
    const isText = cell.kind === GridCellKind.Text || cell.kind === GridCellKind.Number;
    const rightAligned = cell.contentAlign === 'right';
    const highlight = search !== null && search.covers(col) && cell.kind === GridCellKind.Text && !rightAligned;
    if (!isText || cell.contentAlign === 'center' || (!wrap && !highlight) || (rightAligned && !wrap)) {
      drawContent();
      return;
    }
    if (rightAligned) {
      // Wrapped rows align every cell to the top, numbers included.
      ctx.save();
      ctx.font = `${theme.baseFontStyle} ${theme.fontFamily}`;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'right';
      ctx.fillStyle = cell.themeOverride?.textDark ?? theme.textDark;
      ctx.fillText(cell.displayData, rect.x + rect.width - theme.cellHorizontalPadding, rect.y + WRAP_PADDING_Y + WRAP_LINE_HEIGHT / 2);
      ctx.restore();
      return;
    }
    const text = cell.displayData;
    const padX = theme.cellHorizontalPadding;
    const width = Math.max(0, rect.width - padX * 2);
    ctx.save();
    ctx.beginPath();
    ctx.rect(rect.x, rect.y, rect.width, rect.height);
    ctx.clip();
    ctx.font = `${theme.baseFontStyle} ${theme.fontFamily}`;
    ctx.textBaseline = 'middle';
    const measure: Measure = (value) => ctx.measureText(value).width;
    const lines = wrap ? wrapLines(text, width, measure) : [singleLine(text, width, measure)];
    const textColor = cell.themeOverride?.textDark ?? theme.textDark;
    lines.forEach((line, index) => {
      const centerY = wrap
        ? rect.y + WRAP_PADDING_Y + index * WRAP_LINE_HEIGHT + WRAP_LINE_HEIGHT / 2
        : rect.y + rect.height / 2;
      if (highlight && search) {
        ctx.fillStyle = tints.searchHighlight;
        for (const [start, end] of matchRanges(line, search.text)) {
          const x = rect.x + padX + measure(line.slice(0, start));
          ctx.fillRect(x, centerY - WRAP_LINE_HEIGHT / 2 + 1, measure(line.slice(start, end)), WRAP_LINE_HEIGHT - 2);
        }
      }
      ctx.fillStyle = textColor;
      ctx.fillText(line, rect.x + padX, centerY);
    });
    ctx.restore();
  };
}

/** Text a wrapped row lays out: the string cells of the visible columns. */
function rowTexts(row: Row, columns: ReadonlyArray<Pick<ColumnDef, 'id' | 'type'>>): Array<{ index: number; text: string }> {
  const texts: Array<{ index: number; text: string }> = [];
  columns.forEach((column, index) => {
    const value = row.boundedValues?.[column.id]?.preview ?? row.cells[column.id];
    if (typeof value === 'string' && value !== '') texts.push({ index, text: value });
  });
  return texts;
}

/** Per-row heights while wrapping: each row fits its tallest text cell, clamped
 *  to the wrap maximum. Unloaded rows keep the single-line height. */
export function wrappedRowHeights(
  getRow: (index: number) => Row | undefined,
  columns: ReadonlyArray<Pick<ColumnDef, 'id' | 'type'>>,
  widths: readonly number[],
  measure: Measure,
  padX: number,
): (index: number) => number {
  const memo = new Map<string, number>();
  return (index) => {
    const row = getRow(index);
    if (!row) return ROW_HEIGHT;
    const known = memo.get(row.id);
    if (known !== undefined) return known;
    let lines = 1;
    for (const { index: column, text } of rowTexts(row, columns)) {
      const width = Math.max(0, (widths[column] ?? 140) - padX * 2);
      lines = Math.max(lines, wrapLines(text, width, measure).length);
    }
    const height = wrappedRowHeight(lines);
    memo.set(row.id, height);
    return height;
  };
}
