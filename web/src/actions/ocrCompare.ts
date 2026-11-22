/** Optional initial settings when the OCR form opens the upload comparison.
 * Scratch comparison opens empty unless launched from a sheet-backed OCR form. */
export interface OcrCompareTarget {
  engine?: string;
  language?: string;
  dpi?: number;
  searchable_pdf?: boolean;
  sheetId?: string;
  sourceColumn?: string;
  selectedRowIds?: string[];
  maxItems?: number;
  launchId?: number;
}
