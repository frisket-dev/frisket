import { ClipboardPaste, FileText } from 'lucide-react';

interface ImportPasteDetectPanelProps {
  pasteText: string;
  busy: boolean;
  error: string | null;
  onPasteTextChange(value: string): void;
  onDetect(): void;
}

export function ImportPasteDetectPanel({
  pasteText,
  busy,
  error,
  onPasteTextChange,
  onDetect,
}: ImportPasteDetectPanelProps) {
  return (
    <div className="import-paste-step">
      <div className="import-mode-summary">
        <FileText size={16} aria-hidden />
        <strong>Paste rows</strong>
        <span>Copy a table from a spreadsheet or text file</span>
      </div>
      <label className="import-field-label" htmlFor="import-paste-text">
        Rows
      </label>
      <textarea
        id="import-paste-text"
        className="form-input import-url-list import-paste-large"
        data-testid="import-paste-text"
        rows={12}
        value={pasteText}
        placeholder={'name\tscore\nAda Lovelace\t7'}
        onChange={(e) => onPasteTextChange(e.target.value)}
      />
      {error && (
        <div className="import-field-error" role="alert">
          {error}
        </div>
      )}
      <div className="import-step-actions">
        <button
          type="button"
          className="btn btn-primary import-primary"
          data-testid="import-detect-submit"
          disabled={busy || !pasteText.trim()}
          onClick={onDetect}
        >
          <ClipboardPaste size={13} /> {busy ? 'Detecting...' : 'Proceed'}
        </button>
      </div>
    </div>
  );
}
