import type { ImportSessionStatus } from '../../api/open';

export function ImportSessionProgress({
  status,
  selectedFiles,
  active,
  complete,
  preparationPending,
}: {
  status: ImportSessionStatus | null;
  selectedFiles: number;
  active: boolean;
  complete: boolean;
  preparationPending: boolean;
}) {
  if (status === null) {
    return active ? (
      <div className="import-files-progress" role="status">
        <span><span className="run-spinner" aria-hidden /> Importing files…</span>
      </div>
    ) : null;
  }
  if (status.state === 'paused') {
    return <p role="status">Import paused after {status.committed_rows.toLocaleString()} of {status.admitted_files.toLocaleString()} files. Use the Imports notice to retry or cancel.</p>;
  }
  if (status.state === 'cancelling') {
    return <div role="status"><span className="run-spinner" aria-hidden /> Cancelling import… {status.committed_rows.toLocaleString()} files saved.</div>;
  }
  if (status.state === 'cancelled') {
    return <p role="status">Import cancelled. {status.committed_rows.toLocaleString()} saved rows are waiting to be kept or removed in the Imports notice.</p>;
  }
  if (status.state === 'removed') return <p role="status">Added rows were removed.</p>;
  if (!status.sealed) {
    return (
      <div className="import-files-progress" role="status">
        <span><span className="run-spinner" aria-hidden /> Uploading {status.admitted_files.toLocaleString()} of {selectedFiles.toLocaleString()} files</span>
        <progress aria-label="Files uploaded" value={status.admitted_files} max={Math.max(selectedFiles, 1)} />
      </div>
    );
  }
  if (!complete) {
    return (
      <div className="import-files-progress" role="status">
        <span><span className="run-spinner" aria-hidden /> Adding files to the sheet — {status.committed_rows.toLocaleString()} of {status.admitted_files.toLocaleString()} saved</span>
        <progress aria-label="Files saved" value={status.committed_rows} max={Math.max(status.admitted_files, 1)} />
      </div>
    );
  }
  if (preparationPending) {
    return <div role="status"><span className="run-spinner" aria-hidden /> Files saved. Checking text and search setup…</div>;
  }
  return null;
}
