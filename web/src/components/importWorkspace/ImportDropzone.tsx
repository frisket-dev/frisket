import './ImportWorkspaceDialog.css';
import { useEffect, useRef, useState, type DragEvent } from 'react';
import { Upload } from 'lucide-react';
import { snapshotImportFiles } from '../../api/open';
import { Button } from '../Button';
import { canHandlePdfBatch } from './pdfBatch';
import { prepareFileSelection, selectionExpandsArchives, inferDroppedFileMode, useImportUpload } from './fileIntake';
import type { ImportHandlers } from './model';

/** Empty-project state: a table file opens the single global Import workspace
 *  with the original File. Its server-backed reader owns preview and import. */
export function ImportDropzone(props: ImportHandlers & {
  onOpenWorkspace(): void;
  onOpenTable(file: File): void;
  onOpenBulk(files: File[]): void;
  ftmImportEnabled?: boolean;
}) {
  const { onOpenWorkspace, onOpenTable, onOpenBulk, ftmImportEnabled = false, ...handlers } = props;
  const rich = useImportUpload(handlers);
  const [over, setOver] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const preparationGeneration = useRef(0);

  useEffect(() => () => {
    preparationGeneration.current += 1;
  }, []);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (rich.busy || preparing) return;
    const files = snapshotImportFiles(e.dataTransfer.files);
    if (!files.length) return;
    if (!selectionExpandsArchives(files) && canHandlePdfBatch(files)) {
      onOpenBulk(files);
      return;
    }
    if (files.length === 1 && /\.(?:csv|tsv|txt|xlsx|xls|ods)$/i.test(files[0].name)) {
      onOpenTable(files[0]);
      return;
    }
    const generation = ++preparationGeneration.current;
    setPreparing(true);
    void prepareFileSelection(files).then((prepared) => {
      if (generation !== preparationGeneration.current) return;
      const dropMode = inferDroppedFileMode(files, 'csv');
      if (ftmImportEnabled && files.length === 1 && /\.(?:json|jsonl)$/i.test(files[0].name)) {
        void rich.uploadFiles(
          'ftm', files, undefined, undefined, undefined, undefined, undefined, prepared,
        ).then((completion) => { if (completion?.sheetId != null) handlers.onImported(completion.sheetId); });
      } else if (prepared.directNeedsBulkPlan) {
        onOpenBulk(files);
      } else if (dropMode) {
        void rich.uploadFiles(
          dropMode, files, undefined, undefined, undefined, undefined, undefined, prepared,
        ).then((completion) => { if (completion?.sheetId != null) handlers.onImported(completion.sheetId); });
      }
    }).finally(() => {
      if (generation === preparationGeneration.current) setPreparing(false);
    });
  };

  return (
    <div
      className={`dropzone${over ? ' dropzone-over' : ''}`}
      data-testid="import-dropzone"
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
    >
      <Upload size={28} strokeWidth={1.6} />
      <div className="dropzone-title">
        {preparing ? 'Preparing selected files…' : rich.busy ? 'Importing...' : 'Start with data'}
      </div>
      <div className="dropzone-sub">Files, URLs, pasted rows, and feeds</div>
      <Button
        type="button"
        className="btn-primary import-primary dropzone-import-open"
        data-testid="import-workspace-open"
        disabled={rich.busy}
        onClick={() => onOpenWorkspace()}
      >
        <Upload size={13} /> Open import
      </Button>
    </div>
  );
}
