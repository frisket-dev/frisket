import '../components/importWorkspace/ImportWorkspaceDialog.css';
import './MediaCompareShell.css';
// Presentational sub-regions of MediaCompareShell's MediaCompareBody: pure
// render helpers over the compare session — same DOM/classes/testids as when
// they lived inline; the parameterized `t` (testid namespacer) is threaded in so
// each instance keeps its `${testidPrefix}-*` ids.
import { type DragEvent, type ReactNode } from 'react';
import { ChevronLeft, ChevronRight, FilePlus2, Loader2, RotateCw } from 'lucide-react';

import { Button } from '../components/Button';
import { IconButton } from '../components/IconButton';
import type {
  EngineRunState,
  MediaCompareConfig,
  MediaCompareSession,
  ScratchDoc,
  Vote,
} from './mediaCompareSession';

type TestId = (suffix: string) => string;

const DOC_MARK_GLYPH: Record<'neutral' | 'keep' | 'reject' | 'mixed', string> = {
  neutral: '·',
  keep: '✓',
  reject: '✗',
  mixed: '±',
};

const VOTE_GLYPH: Record<Vote, string> = { neutral: '·', keep: '✓', reject: '✗' };

/** Shared vote control for every comparison result lane. */
export function CompareVoteControl({
  testId,
  engineId,
  vote,
  label,
  summary,
  onVote,
}: {
  testId: string;
  engineId?: string | null;
  vote: Vote;
  label: string;
  summary?: ReactNode;
  onVote(): void;
}) {
  return (
    <button
      type="button"
      className="ocr-compare-vote-chip"
      data-testid={testId}
      data-engine-id={engineId ?? ''}
      data-vote={vote}
      title="Vote: click to cycle keep / reject"
      onClick={onVote}
    >
      <span className="ocr-compare-vote-glyph">{VOTE_GLYPH[vote]}</span>
      <span className="ocr-compare-vote-label">{label}</span>
      {summary ? <span className="ocr-compare-col-summary muted mono">{summary}</span> : null}
    </button>
  );
}

/** Pending/running/error presentation shared by media and topic result lanes.
 * Completed content stays domain-owned and is supplied as children. */
export function CompareResultState<R>({
  run,
  engineLabel,
  pendingLabel,
  runningLabel,
  pendingTestId,
  runningTestId,
  errorTestId,
  retryTestId,
  onRetry,
  children,
}: {
  run: EngineRunState<R> | undefined;
  engineLabel: string;
  pendingLabel: ReactNode;
  runningLabel: ReactNode;
  pendingTestId?: string;
  runningTestId?: string;
  errorTestId?: string;
  retryTestId?: string;
  onRetry?(): void;
  children: ReactNode;
}) {
  if (!run) {
    return (
      <div className="ocr-compare-engine-pending muted" data-testid={pendingTestId}>
        {pendingLabel}
      </div>
    );
  }
  if (run.status === 'running') {
    return (
      <div className="ocr-compare-engine-running muted" data-testid={runningTestId}>
        <Loader2 size={13} className="ocr-compare-spin" aria-hidden /> {runningLabel}
      </div>
    );
  }
  if (run.status === 'error') {
    return (
      <div className="ocr-compare-engine-error" data-testid={errorTestId} role="alert">
        <strong>{engineLabel} failed</strong>
        <span>{run.message}</span>
        {onRetry ? (
          <Button
            type="button"
            className="ocr-compare-engine-retry"
            data-testid={retryTestId}
            onClick={onRetry}
          >
            <RotateCw size={12} /> Retry
          </Button>
        ) : null}
      </div>
    );
  }
  return <>{children}</>;
}

/** Exactly one pre-result prompt: no variants, configured but unavailable,
 * or ready to run. */
export function CompareColumnsState({
  testIdPrefix,
  configuredCount,
  runnableCount,
  showReady,
  noVariantsLabel = 'No variants — add an engine to compare.',
  unavailableLabel = 'No engine is ready — configure a variant to finish setup.',
}: {
  testIdPrefix: string;
  configuredCount: number;
  runnableCount: number;
  showReady: boolean;
  noVariantsLabel?: ReactNode;
  unavailableLabel?: ReactNode;
}) {
  if (runnableCount === 0) {
    return (
      <div
        className="ocr-compare-empty-variants muted"
        data-testid={`${testIdPrefix}-empty-variants`}
      >
        {configuredCount === 0 ? noVariantsLabel : unavailableLabel}
      </div>
    );
  }
  if (!showReady) return null;
  return (
    <div className="ocr-compare-run-hint" data-testid={`${testIdPrefix}-run-hint`}>
      Ready to compare — press <strong>Run</strong>.
    </div>
  );
}

export interface CompareFooterDifference {
  label: ReactNode;
  previousLabel: string;
  nextLabel: string;
  onPrevious(): void;
  onNext(): void;
  previousTestId?: string;
  nextTestId?: string;
}

/** Shared comparison footer frame and previous/next controls. */
export function CompareFooter({
  footerTestId,
  navigationTestId,
  verdictTestId,
  verdict,
  difference,
  fallback,
}: {
  footerTestId: string;
  navigationTestId: string;
  verdictTestId: string;
  verdict: string;
  difference: CompareFooterDifference | null;
  fallback: ReactNode;
}) {
  return (
    <footer className="ocr-compare-footer" data-testid={footerTestId}>
      <div className="ocr-compare-token-nav" data-testid={navigationTestId}>
        {difference ? (
          <>
            <IconButton
              icon={ChevronLeft}
              size="inline"
              className="icon-btn"
              data-testid={difference.previousTestId}
              label={difference.previousLabel}
              onClick={difference.onPrevious}
            />
            <span className="mono">{difference.label}</span>
            <IconButton
              icon={ChevronRight}
              size="inline"
              className="icon-btn"
              data-testid={difference.nextTestId}
              label={difference.nextLabel}
              onClick={difference.onNext}
            />
          </>
        ) : (
          <span className="mono muted">{fallback}</span>
        )}
      </div>
      <div className="ocr-compare-verdict mono" data-testid={verdictTestId}>
        Verdict so far: {verdict}
      </div>
    </footer>
  );
}

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
