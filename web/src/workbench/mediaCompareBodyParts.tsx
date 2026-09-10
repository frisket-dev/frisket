// Presentational sub-regions of MediaCompareShell's MediaCompareBody: pure
// render helpers over the compare session — same DOM/classes/testids as when
// they lived inline; the parameterized `t` (testid namespacer) is threaded in so
// each instance keeps its `${testidPrefix}-*` ids.
import { type DragEvent } from 'react';
import { FilePlus2 } from 'lucide-react';

import type {
  MediaCompareConfig,
  MediaCompareSession,
  ScratchDoc,
} from './mediaCompareSession';

type TestId = (suffix: string) => string;

const DOC_MARK_GLYPH: Record<'neutral' | 'keep' | 'reject' | 'mixed', string> = {
  neutral: '·',
  keep: '✓',
  reject: '✗',
  mixed: '±',
};

function docVoteMark(doc: ScratchDoc<unknown>): 'neutral' | 'keep' | 'reject' | 'mixed' {
  const votes = Object.values(doc.votes);
  const keeps = votes.filter((vote) => vote === 'keep').length;
  const rejects = votes.filter((vote) => vote === 'reject').length;
  if (keeps > 0 && rejects > 0) return 'mixed';
  if (keeps > 0) return 'keep';
  if (rejects > 0) return 'reject';
  return 'neutral';
}

/** The empty-state dropzone shown before any media is added. */
export function CompareFrontDoor<R>({
  t,
  session,
  config,
  fileInputRef,
  onDropZone,
  frontDoorTitle,
  frontDoorHint,
}: {
  t: TestId;
  session: MediaCompareSession<R>;
  config: MediaCompareConfig<R>;
  fileInputRef: React.RefObject<HTMLInputElement>;
  onDropZone(event: DragEvent): void;
  frontDoorTitle: string;
  frontDoorHint: string;
}) {
  const { runnableColumns, ingestFiles, engineLabel } = session;
  return (
    // Real <button>, not a role="button" div: this dropzone is not nested
    // inside another interactive ancestor (unlike App.tsx's sheet-info span,
    // which stays role="button" for exactly that reason). A real <button>
    // gets Enter/Space activation for free, so the manual onKeyDown that
    // used to call fileInputRef.current?.click() is DELETED here, not kept alongside —
    // keeping it would double-fire the file picker on every keyboard
    // activation (native button synthesizes a click on Enter/Space, which
    // still runs onClick below).
    <button
      type="button"
      className="ocr-compare-frontdoor dropzone"
      data-testid={t('dropzone')}
      aria-label={frontDoorTitle}
      onDragOver={(event) => event.preventDefault()}
      onDrop={onDropZone}
      onClick={() => fileInputRef.current?.click()}
    >
      <FilePlus2 size={26} aria-hidden />
      <strong>{frontDoorTitle}</strong>
      <span className="muted">{frontDoorHint}</span>
      <div className="ocr-compare-frontdoor-engines">
        {runnableColumns.map((column) => (
          <span key={column.id} className="engine-tier-chip">
            {engineLabel(column.engineId)}
          </span>
        ))}
      </div>
      <input
        ref={fileInputRef}
        type="file"
        accept={config.accept}
        multiple
        hidden
        aria-label={frontDoorTitle}
        data-testid={t('file-input')}
        onChange={(event) => {
          ingestFiles(Array.from(event.target.files ?? []));
          event.target.value = '';
        }}
      />
    </button>
  );
}

/** The left-hand doc list + "drop more" affordance. */
export function CompareDocList<R>({
  t,
  session,
  config,
  moreInputRef,
  onDropZone,
}: {
  t: TestId;
  session: MediaCompareSession<R>;
  config: MediaCompareConfig<R>;
  moreInputRef: React.RefObject<HTMLInputElement>;
  onDropZone(event: DragEvent): void;
}) {
  const { docs, activeDoc, setActiveDocId, ingestFiles } = session;
  return (
    <aside className="ocr-compare-doc-list" data-testid={t('doc-list')}>
      {docs.map((doc) => {
        const mark = docVoteMark(doc);
        return (
          <button
            type="button"
            key={doc.id}
            className={`ocr-compare-doc-item${doc.id === activeDoc?.id ? ' active' : ''}`}
            data-testid={t('doc-item')}
            data-doc-vote={mark}
            onClick={() => setActiveDocId(doc.id)}
          >
            <span className="ocr-compare-doc-mark" data-vote={mark}>
              {DOC_MARK_GLYPH[mark]}
            </span>
            <span className="ocr-compare-doc-text">
              <span className="ocr-compare-doc-title">{doc.filename}</span>
              <span className="ocr-compare-doc-secondary muted">{config.docSecondary(doc)}</span>
            </span>
          </button>
        );
      })}
      <button
        type="button"
        className="ocr-compare-drop-more dropzone"
        data-testid={t('drop-more')}
        onDragOver={(event) => event.preventDefault()}
        onDrop={onDropZone}
        onClick={() => moreInputRef.current?.click()}
      >
        <FilePlus2 size={13} /> drop more
      </button>
      <input
        ref={moreInputRef}
        type="file"
        accept={config.accept}
        multiple
        hidden
        aria-label="Add more files"
        data-testid={t('file-input-more')}
        onChange={(event) => {
          ingestFiles(Array.from(event.target.files ?? []));
          event.target.value = '';
        }}
      />
    </aside>
  );
}
