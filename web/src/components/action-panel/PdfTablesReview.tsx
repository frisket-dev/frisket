import { useEffect, useMemo, useState } from 'react';
import type { SheetMeta } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { PdfTablesPicker } from '../PdfTablesPicker';

export interface PdfTablesMaterializeIntent {
  columnId: string;
  columnName: string;
  includeColumns: string[];
  targetName: string;
}

export interface PdfTablesExportIntent {
  column: { id: string; name: string };
  groupBy: string;
  excludeColumns: string[];
}

export interface PdfTablesReviewProps {
  actionKind: string;
  sheet: Pick<SheetMeta, 'id' | 'columns'>;
  outputName: string;
  onMaterialize(intent: PdfTablesMaterializeIntent): void;
  onExport?(intent: PdfTablesExportIntent): void;
}

/** Post-extraction entry point for reviewing and materializing PDF tables.
 *
 * This component owns only presentation state and emits semantic intents. The
 * parent remains responsible for projecting those intents into action requests.
 */
export function PdfTablesReview({
  actionKind,
  sheet,
  outputName,
  onMaterialize,
  onExport,
}: PdfTablesReviewProps) {
  const reviewColumn = useMemo(() => {
    if (actionKind !== 'media.extract_pdf_tables') return null;
    const targetName = outputName.trim();
    return sheet.columns.find((column) => column.type === 'json' && column.name === targetName) ?? null;
  }, [actionKind, outputName, sheet.columns]);
  if (!reviewColumn) return null;
  return <PdfTablesColumnReview
    key={`${sheet.id}:${reviewColumn.id}:${reviewColumn.currentRunId}:${reviewColumn.latestRunId}`}
    sheetId={sheet.id} reviewColumn={reviewColumn} outputName={outputName}
    onMaterialize={onMaterialize} onExport={onExport} />;
}

function PdfTablesColumnReview({ sheetId, reviewColumn, outputName, onMaterialize, onExport }:
  Omit<PdfTablesReviewProps, 'actionKind' | 'sheet'> & {
    sheetId: string;
    reviewColumn: SheetMeta['columns'][number];
  }) {
  const { projectApi } = useWorkspaceStores();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [verified, setVerified] = useState(false);
  useEffect(() => {
    let current = true;
    projectApi.getColumnRuns(reviewColumn.id, 0, 1).then((history) => {
      // This is an advisory review entry point, not current-cell provenance or
      // permission to feed a list. Mixed/re-run values remain backend-validated.
      const producer = history.currentRun ?? history.latestRun;
      if (current && producer?.actionKind === 'media.extract_pdf_tables') {
        setVerified(true);
      }
    }).catch(() => { /* A failed provenance lookup cannot claim an extraction. */ });
    return () => { current = false; };
  }, [projectApi, reviewColumn.id]);

  if (!verified) return null;

  return (
    <>
      <div className="pdf-tables-review-banner" data-testid="pdf-tables-review-banner">
        <span className="pdf-tables-review-text">
          Review extracted tables before building a sheet. Current values may include edits or multiple runs.
        </span>
        <button
          type="button"
          className="btn"
          data-testid="review-tables"
          onClick={() => setPickerOpen(true)}
        >
          Review tables →
        </button>
      </div>
      {pickerOpen && (
        <div className="pdf-tables-picker-overlay">
          <PdfTablesPicker
            sheetId={sheetId}
            column={{ id: reviewColumn.id, name: reviewColumn.name }}
            defaultTargetName={outputName.trim() || reviewColumn.name}
            onMaterialize={(intent) => {
              setPickerOpen(false);
              onMaterialize({ ...intent, columnId: reviewColumn.id });
            }}
            onExportTables={onExport
              ? (intent) => {
                setPickerOpen(false);
                onExport({
                  column: { id: reviewColumn.id, name: reviewColumn.name },
                  groupBy: intent.groupBy,
                  excludeColumns: intent.excludeColumns,
                });
              }
              : undefined}
            onClose={() => setPickerOpen(false)}
          />
        </div>
      )}
    </>
  );
}
