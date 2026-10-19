import '../Modal.css';
import '../FormControls.css';
import './ImportWorkspaceDialog.css';
import './ImportModeRail.css';
import { AtSign, ListVideo } from 'lucide-react';
import { Button } from '../Button';
import { describeColumns, type NerImportOffer } from '../nerImportPromptModel';

export function MediaDownloadPrompt({
  columnName,
  onRun,
  onDismiss,
}: {
  columnName: string;
  onRun(): void;
  onDismiss(): void;
}) {
  return (
    <div className="content-stack import-feed-form" data-testid="media-download-prompt">
      <div className="import-mode-summary">
        <ListVideo size={16} aria-hidden />
        <strong>Download media?</strong>
      </div>
      <p className="modal-body-text">
        <strong>{columnName}</strong> looks like it has media links that
        haven't been downloaded yet. Download now, or skip it — you can
        always run it later from the column's header menu.
      </p>
      <div className="import-step-actions">
        <Button
          type="button"

          data-testid="media-download-dismiss"
          onClick={onDismiss}
        >
          Not now
        </Button>
        <Button
          type="button"
          className="btn-primary import-primary"
          data-testid="media-download-run"
          onClick={onRun}
        >
          <ListVideo size={13} />
          Download
        </Button>
      </div>
    </div>
  );
}

export function NerExtractPrompt({
  offer,
  onRun,
  onDismiss,
}: {
  offer: NerImportOffer;
  onRun(): void;
  onDismiss(): void;
}) {
  return (
    <div className="content-stack import-feed-form" data-testid="ner-import-prompt">
      <div className="import-mode-summary">
        <AtSign size={16} aria-hidden />
        <strong>Find the names in it?</strong>
      </div>
      <p className="modal-body-text">
        Imported {offer.rowCount.toLocaleString()}{' '}
        {offer.rowCount === 1 ? 'row' : 'rows'} of text in{' '}
        <strong>{describeColumns(offer.columnNames)}</strong>. Pull out the
        people, organizations, places, dates and amounts mentioned in{' '}
        {offer.rowCount === 1 ? 'it' : 'them'}, so you can browse and filter by
        who and what appears
        {offer.free ? ', using a model that runs on this machine' : ''}?
      </p>
      <div className="import-step-actions">
        <Button
          type="button"

          data-testid="ner-import-dismiss"
          onClick={onDismiss}
        >
          Not now
        </Button>
        <Button
          type="button"
          className="btn-primary import-primary"
          data-testid="ner-import-run"
          onClick={onRun}
        >
          <AtSign size={13} />
          Extract entities
        </Button>
      </div>
    </div>
  );
}
