/** Optional initial settings when the OCR form opens the upload comparison.
 * Comparison media always comes from the user's dropped files. */
export interface OcrCompareTarget {
  engine?: string;
  language?: string;
  dpi?: number;
  searchable_pdf?: boolean;
}
