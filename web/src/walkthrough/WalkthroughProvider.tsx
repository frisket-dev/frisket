import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { useNativePopover } from '../hooks/useNativePopover';
import { topLayerPortalRoot } from '../topLayerPortal';
import { WalkthroughContext, type WalkthroughContextValue } from './context';
import { resolveWalkthroughTarget } from './targets';
import { useEditionModule } from '../editions/module';
import styles from './WalkthroughProvider.module.css';
import {
  type WalkthroughDefinition,
  type WalkthroughInstruction,
} from './walkthroughs';

interface CardPosition {
  top: number;
  left: number;
  centered?: boolean;
}

interface TargetBox {
  top: number;
  left: number;
  width: number;
  height: number;
}

const CARD_WIDTH = 340;
const CARD_HEIGHT_ESTIMATE = 210;
const CARD_GAP = 14;
const VIEWPORT_MARGIN = 16;
const ACTION_DRAWER_OVERLAP = 10;
const TARGET_BOX_GAP = 3;
const TARGET_POLL_INTERVAL_MS = 500;
const CLICK_ADVANCE_DELAY_MS = 100;
const GUIDE_SEEN_STORAGE_KEY = 'frisket.walkthrough.guide-seen.v1';
const SAMPLE_INTRO_SEEN_STORAGE_KEY = 'frisket.walkthrough.sample-project.intro-seen.v1';
const SAMPLE_HINT_DISMISSED_STORAGE_KEY = 'frisket.walkthrough.sample-project.hint-dismissed.v1';
const ACTIVE_RUN_STORAGE_PREFIX = 'frisket.walkthrough.active-run.v1:';
const ACTIVE_RUN_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function revealWalkthroughTarget(target: HTMLElement): void {
  const drawer = target.closest<HTMLElement>('[data-testid="action-drawer"]');
  const scrollHost = drawer?.querySelector<HTMLElement>(
    '.action-panel.action-drawer-form-host',
  );
  if (!drawer || !scrollHost) {
    target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return;
  }

  const targetRect = target.getBoundingClientRect();
  const hostRect = scrollHost.getBoundingClientRect();
  const headerBottom = drawer
    .querySelector<HTMLElement>('.action-drawer-header')
    ?.getBoundingClientRect().bottom ?? hostRect.top;
  const footerTop = drawer
    .querySelector<HTMLElement>('.action-run-actions')
    ?.getBoundingClientRect().top ?? hostRect.bottom;
  const visibleTop = Math.max(hostRect.top, headerBottom);
  const visibleBottom = Math.min(hostRect.bottom, footerTop);
  if (targetRect.top < visibleTop || targetRect.bottom > visibleBottom) {
    target.scrollIntoView({ block: 'center', inline: 'nearest' });
  }
}

function storedGuideSeen(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(GUIDE_SEEN_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

function storeGuideSeen(): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(GUIDE_SEEN_STORAGE_KEY, 'true');
  } catch {
    // Storage can be unavailable in privacy modes; session state still works.
  }
}

function storedFlag(key: string): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(key) === 'true';
  } catch {
    return false;
  }
}

function storeFlag(key: string): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, 'true');
  } catch {
    // Storage can be unavailable in privacy modes; session state still works.
  }
}

function cardPositionFor(
  target: HTMLElement | null,
  cardHeight = CARD_HEIGHT_ESTIMATE,
): CardPosition {
  if (!target) {
    return {
      top: Math.max(VIEWPORT_MARGIN, (window.innerHeight - cardHeight) / 2),
      left: Math.max(VIEWPORT_MARGIN, (window.innerWidth - CARD_WIDTH) / 2),
      centered: true,
    };
  }
  const rect = target.getBoundingClientRect();
  const maxLeft = Math.max(VIEWPORT_MARGIN, window.innerWidth - CARD_WIDTH - VIEWPORT_MARGIN);
  const maxTop = Math.max(VIEWPORT_MARGIN, window.innerHeight - cardHeight - VIEWPORT_MARGIN);
  if (target.dataset.walkthroughGridColumn) {
    const leftOfColumn = rect.left - CARD_WIDTH - CARD_GAP;
    const besideColumn = leftOfColumn >= VIEWPORT_MARGIN
      ? leftOfColumn
      : rect.right + CARD_GAP;
    return {
      top: Math.min(maxTop, Math.max(VIEWPORT_MARGIN, rect.top + VIEWPORT_MARGIN)),
      left: Math.min(maxLeft, Math.max(VIEWPORT_MARGIN, besideColumn)),
    };
  }
  const actionDrawer = target.closest<HTMLElement>('[data-testid="action-drawer"]');
  if (actionDrawer) {
    const drawerRect = actionDrawer.getBoundingClientRect();
    return {
      top: Math.min(maxTop, Math.max(VIEWPORT_MARGIN, rect.top)),
      left: Math.min(
        maxLeft,
        Math.max(VIEWPORT_MARGIN, drawerRect.left - CARD_WIDTH + ACTION_DRAWER_OVERLAP),
      ),
    };
  }
  const below = rect.bottom + CARD_GAP;
  const above = rect.top - cardHeight - CARD_GAP;
  const top = below + cardHeight <= window.innerHeight - VIEWPORT_MARGIN
    ? below
    : Math.max(VIEWPORT_MARGIN, above);
  return {
    top,
    left: Math.min(maxLeft, Math.max(VIEWPORT_MARGIN, rect.left)),
  };
}

function clampCardPosition(position: CardPosition, height = CARD_HEIGHT_ESTIMATE): CardPosition {
  const maxLeft = Math.max(VIEWPORT_MARGIN, window.innerWidth - CARD_WIDTH - VIEWPORT_MARGIN);
  const maxTop = Math.max(VIEWPORT_MARGIN, window.innerHeight - height - VIEWPORT_MARGIN);
  return {
    top: Math.min(maxTop, Math.max(VIEWPORT_MARGIN, position.top)),
    left: Math.min(maxLeft, Math.max(VIEWPORT_MARGIN, position.left)),
  };
}

function targetBoxFor(target: HTMLElement): TargetBox {
  const rect = target.getBoundingClientRect();
  return {
    top: rect.top - TARGET_BOX_GAP,
    left: rect.left - TARGET_BOX_GAP,
    width: rect.width + TARGET_BOX_GAP * 2,
    height: rect.height + TARGET_BOX_GAP * 2,
  };
}

function WalkthroughHost({
  walkthrough,
  stepIndex,
  onStepIndexChange,
  onPause,
  onFinish,
}: {
  walkthrough: WalkthroughDefinition;
  stepIndex: number;
  onStepIndexChange(stepIndex: number): void;
  onPause(): void;
  onFinish(): void;
}) {
  const [target, setTarget] = useState<HTMLElement | null>(null);
  const [targetBox, setTargetBox] = useState<TargetBox | null>(null);
  const [position, setPosition] = useState<CardPosition>(() => cardPositionFor(null));
  const [dragging, setDragging] = useState(false);
  const advanceTimer = useRef<number | null>(null);
  const cardRef = useRef<HTMLElement | null>(null);
  const manuallyPositioned = useRef(false);
  const dragState = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    startTop: number;
    startLeft: number;
  } | null>(null);
  const step = walkthrough.steps[stepIndex];
  const isFinal = stepIndex === walkthrough.steps.length - 1;

  useNativePopover(cardRef, onPause, { escape: false, outside: false });

  const advance = useCallback(() => {
    if (stepIndex >= walkthrough.steps.length - 1) {
      onFinish();
      return;
    }
    onStepIndexChange(stepIndex + 1);
  }, [onFinish, onStepIndexChange, stepIndex, walkthrough.steps.length]);

  useEffect(() => {
    const resolve = () => setTarget(resolveWalkthroughTarget(step.target));
    resolve();
    const observer = new MutationObserver(resolve);
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ['aria-hidden', 'class', 'data-testid', 'hidden', 'style'],
      childList: true,
      subtree: true,
    });
    const poll = window.setInterval(resolve, TARGET_POLL_INTERVAL_MS);
    return () => {
      observer.disconnect();
      window.clearInterval(poll);
    };
  }, [step.target]);

  useLayoutEffect(() => {
    const measure = () => {
      setPosition((current) => (
        manuallyPositioned.current
          ? clampCardPosition(current, cardRef.current?.offsetHeight || CARD_HEIGHT_ESTIMATE)
          : cardPositionFor(
              target,
              cardRef.current?.offsetHeight || CARD_HEIGHT_ESTIMATE,
            )
      ));
      setTargetBox(target ? targetBoxFor(target) : null);
    };
    let measureFrame: number | null = null;
    if (target) {
      const gridColumnName = target.dataset.walkthroughGridColumn;
      if (gridColumnName) {
        window.dispatchEvent(new CustomEvent('frisket:reveal-grid-column', {
          detail: { columnName: gridColumnName },
        }));
      }
      // Never leave the highlight at a clipped, pre-scroll position. Drawer
      // forms have sticky headers and footers, so reveal the control within
      // the genuinely visible middle band and measure after layout settles.
      revealWalkthroughTarget(target);
      measure();
      measureFrame = window.requestAnimationFrame(measure);
    } else {
      measure();
    }
    const resizeObserver = target && typeof ResizeObserver === 'function'
      ? new ResizeObserver(measure)
      : null;
    if (target) resizeObserver?.observe(target);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      resizeObserver?.disconnect();
      if (measureFrame !== null) window.cancelAnimationFrame(measureFrame);
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [target]);

  // A modal dialog opened by the highlighted control enters the browser's top
  // layer after this card. Re-show the manual popover when a step/target changes
  // so the instruction card remains the newest (topmost) top-layer element.
  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card || typeof card.showPopover !== 'function') return;
    try {
      if (card.matches(':popover-open')) card.hidePopover();
      card.showPopover();
    } catch {
      // The z-index fallback remains usable in browsers without Popover API.
    }
  }, [stepIndex, target]);

  useEffect(() => {
    if (!target || step.advance !== 'click-target') return undefined;
    const onTargetClick = () => {
      if (advanceTimer.current !== null) window.clearTimeout(advanceTimer.current);
      // Let the click update the ribbon/menu/drawer before resolving the next target.
      advanceTimer.current = window.setTimeout(() => {
        advanceTimer.current = null;
        advance();
      }, CLICK_ADVANCE_DELAY_MS);
    };
    target.addEventListener('click', onTargetClick, { capture: true });
    return () => {
      target.removeEventListener('click', onTargetClick, { capture: true });
    };
  }, [advance, step.advance, target]);

  // A click may replace its own target immediately (opening a drawer, changing
  // a view, or switching sheets). Do not cancel that click's pending advance
  // when the target listener is replaced; only cancel when the step itself
  // changes or the walkthrough unmounts.
  useEffect(() => () => {
    if (advanceTimer.current !== null) {
      window.clearTimeout(advanceTimer.current);
      advanceTimer.current = null;
    }
  }, [stepIndex]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onPause();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onPause]);

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest('button')) return;
    manuallyPositioned.current = true;
    dragState.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startTop: position.top,
      startLeft: position.left,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDragging(true);
    event.preventDefault();
  };

  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragState.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setPosition(clampCardPosition({
      top: drag.startTop + event.clientY - drag.startY,
      left: drag.startLeft + event.clientX - drag.startX,
    }, cardRef.current?.offsetHeight || CARD_HEIGHT_ESTIMATE));
  };

  const finishDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragState.current?.pointerId !== event.pointerId) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    dragState.current = null;
    setDragging(false);
  };

  return createPortal(
    <>
      {targetBox && (
        <div
          className="walkthrough-target-box"
          data-testid="walkthrough-target-box"
          data-target-testid={target?.dataset.testid}
          aria-hidden="true"
          style={targetBox}
        />
      )}
      <aside
        ref={cardRef}
        className={`walkthrough-card${position.centered ? ' walkthrough-card-centered' : ''}${dragging ? ' dragging' : ''}`}
        data-testid="walkthrough-card"
        role="dialog"
        aria-modal="false"
        aria-labelledby="walkthrough-step-title"
        style={{ top: position.top, left: position.left }}
      >
        <div
          className="walkthrough-card-head walkthrough-drag-handle"
          data-testid="walkthrough-drag-handle"
          title="Drag to move"
          onPointerDown={startDrag}
          onPointerMove={moveDrag}
          onPointerUp={finishDrag}
          onPointerCancel={finishDrag}
        >
          <span className="walkthrough-progress">
            {stepIndex + 1} of {walkthrough.steps.length}
          </span>
          <button
            type="button"
            className="icon-btn walkthrough-close"
            aria-label="Pause walkthrough"
            onClick={onPause}
          >
            <X size={15} />
          </button>
        </div>
        <h2 id="walkthrough-step-title">{step.title}</h2>
        <WalkthroughInstructionCopy instruction={step.instruction} />
        {step.expected && <p className="walkthrough-expected">Then: {step.expected}</p>}
        {!target && (step.advance === 'click-target' || step.target.kind === 'completed-run') && (
          <p className="walkthrough-unavailable" role="status">
            {step.target.kind === 'completed-run'
              ? 'Waiting for the action to finish every row. The guide will keep checking; use Next if you want to continue manually.'
              : 'This control is not visible yet. The guide will keep checking, or you can use Back or Next.'}
          </p>
        )}
        <div className="walkthrough-card-actions">
          <button
            type="button"
            className="btn"
            disabled={stepIndex === 0}
            onClick={() => onStepIndexChange(Math.max(0, stepIndex - 1))}
          >
            Back
          </button>
          {step.advance === 'click-target' && (
            <span className="walkthrough-click-hint">Click the highlighted control</span>
          )}
          <button type="button" className="btn btn-primary" onClick={advance}>
            {isFinal ? 'Finish' : 'Next'}
          </button>
        </div>
      </aside>
    </>,
    topLayerPortalRoot(),
  );
}

function WalkthroughInstructionCopy({
  instruction,
}: {
  instruction: WalkthroughInstruction;
}) {
  if (typeof instruction === 'string') return <p>{instruction}</p>;
  return (
    <p>
      {instruction.lead} <code>{instruction.code}</code>.
      {instruction.explanation && <> {instruction.explanation}</>}
      {instruction.learnMore && (
        <>
          {' '}
          <a href={instruction.learnMore.href} target="_blank" rel="noopener noreferrer">
            {instruction.learnMore.label}
          </a>
          .
        </>
      )}
    </p>
  );
}

function WalkthroughRows({
  walkthroughs,
  walkthroughBadges,
  onStart,
  testIdPrefix,
  autoFocus = false,
}: {
  walkthroughs: readonly WalkthroughDefinition[];
  walkthroughBadges: readonly { id: string; badges: readonly string[] }[];
  onStart(id: string): void;
  testIdPrefix: string;
  autoFocus?: boolean;
}) {
  return (
    <div className={styles.compactChoices}>
      {walkthroughs.map((candidate, index) => (
        <button
          key={candidate.id}
          type="button"
          className={styles.compactChoice}
          data-testid={`${testIdPrefix}${candidate.id}`}
          autoFocus={autoFocus && index === 0}
          onClick={() => onStart(candidate.id)}
        >
          <span>
            <strong>{candidate.title}</strong>
            <small>{candidate.description}</small>
          </span>
          <span className={styles.badgeList}>
            {walkthroughBadges.find((item) => item.id === candidate.id)?.badges.map((badge) => (
              <small key={badge} className={styles.walkthroughBadge}>{badge}</small>
            ))}
          </span>
        </button>
      ))}
    </div>
  );
}

function WalkthroughChooser({
  walkthroughs,
  walkthroughBadges,
  onStart,
  onClose,
}: {
  walkthroughs: readonly WalkthroughDefinition[];
  walkthroughBadges: readonly { id: string; badges: readonly string[] }[];
  onStart(id: string): void;
  onClose(): void;
}) {
  const chooserRef = useRef<HTMLElement>(null);
  useNativePopover(chooserRef, onClose, { escape: false, outside: false });

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return createPortal(
    <aside
      ref={chooserRef}
      className="walkthrough-chooser"
      data-testid="walkthrough-chooser"
      role="dialog"
      aria-modal="false"
      aria-labelledby="walkthrough-chooser-title"
    >
      <div className="walkthrough-card-head">
        <span className="walkthrough-progress">Guided investigations</span>
        <button
          type="button"
          className="icon-btn walkthrough-close"
          aria-label="Close walkthrough chooser"
          onClick={onClose}
        >
          <X size={15} />
        </button>
      </div>
      <h2 id="walkthrough-chooser-title">What would you like to try?</h2>
      <WalkthroughRows
        walkthroughs={walkthroughs}
        walkthroughBadges={walkthroughBadges}
        onStart={onStart}
        testIdPrefix="walkthrough-choice-"
        autoFocus
      />
    </aside>,
    topLayerPortalRoot(),
  );
}

function SampleProjectIntro({
  walkthroughs,
  walkthroughBadges,
  onStart,
  onPokeAround,
}: {
  walkthroughs: readonly WalkthroughDefinition[];
  walkthroughBadges: readonly { id: string; badges: readonly string[] }[];
  onStart(id: string): void;
  onPokeAround(): void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [guideRect, setGuideRect] = useState<TargetBox | null>(null);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || typeof dialog.showModal !== 'function') return;
    if (!dialog.open) dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
    };
  }, []);

  useLayoutEffect(() => {
    const measure = () => {
      const guide = document.querySelector<HTMLElement>('[data-testid="chrome-walkthrough"]');
      setGuideRect(guide ? targetBoxFor(guide) : null);
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, []);

  return createPortal(
    <dialog
      ref={dialogRef}
      className={styles.sampleIntro}
      data-testid="sample-project-intro"
      role="dialog"
      aria-modal="true"
      aria-labelledby="sample-project-intro-title"
      onCancel={(event) => {
        event.preventDefault();
        onPokeAround();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onPokeAround();
      }}
    >
      {guideRect && (
        <div
          aria-hidden="true"
          className={styles.guideSpotlight}
          data-testid="sample-guide-spotlight"
          style={guideRect}
        />
      )}
      <section className={styles.sampleIntroPanel}>
        <div className={styles.sampleIntroChoices}>
          <p className={styles.sampleIntroEyebrow}>SAMPLE PROJECT</p>
          <h2 id="sample-project-intro-title">How do you want to start?</h2>
          <div className={`${styles.startOption} ${styles.startOptionSelected}`}>
            <strong>Guide me</strong>
            <span>Choose a walkthrough on the right.</span>
          </div>
          <button
            type="button"
            className={styles.startOption}
            aria-label="Poke around first"
            onClick={onPokeAround}
          >
            <strong>Poke around first</strong>
            <span>Come back any time via Guide ↗</span>
          </button>
        </div>
        <div className={styles.sampleIntroList}>
          <button
            type="button"
            className={styles.introClose}
            aria-label="Close introduction"
            onClick={onPokeAround}
          >
            <X size={15} />
          </button>
          <h3>Walkthroughs</h3>
          <WalkthroughRows
            walkthroughs={walkthroughs}
            walkthroughBadges={walkthroughBadges}
            onStart={onStart}
            testIdPrefix="sample-walkthrough-choice-"
            autoFocus
          />
        </div>
      </section>
    </dialog>,
    topLayerPortalRoot(),
  );
}

interface WalkthroughRun {
  id: number;
  walkthrough: WalkthroughDefinition;
  stepIndex: number;
  projectId?: string;
  updatedAt: number;
}

interface StoredWalkthroughRun {
  walkthroughId: string;
  stepIndex: number;
  projectId: string;
  updatedAt: number;
}

function activeRunStorageKey(projectId: string): string {
  return `${ACTIVE_RUN_STORAGE_PREFIX}${projectId}`;
}

function clearStoredRun(projectId: string | undefined): void {
  if (!projectId || typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(activeRunStorageKey(projectId));
  } catch {
    // Storage can be unavailable in privacy modes; session state still works.
  }
}

function storeRun(run: WalkthroughRun): void {
  if (!run.projectId || typeof window === 'undefined') return;
  const stored: StoredWalkthroughRun = {
    walkthroughId: run.walkthrough.id,
    stepIndex: run.stepIndex,
    projectId: run.projectId,
    updatedAt: run.updatedAt,
  };
  try {
    window.localStorage.setItem(
      activeRunStorageKey(run.projectId),
      JSON.stringify(stored),
    );
  } catch {
    // Storage can be unavailable in privacy modes; session state still works.
  }
}

function restoreStoredRun(
  projectId: string | undefined,
  walkthroughs: readonly WalkthroughDefinition[],
): Omit<WalkthroughRun, 'id'> | null {
  if (!projectId || typeof window === 'undefined') return null;
  const key = activeRunStorageKey(projectId);
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Partial<StoredWalkthroughRun>;
    const walkthrough = walkthroughs.find(
      (candidate) => candidate.id === stored.walkthroughId,
    );
    const now = Date.now();
    if (
      stored.projectId !== projectId
      || !Number.isInteger(stored.stepIndex)
      || typeof stored.stepIndex !== 'number'
      || stored.stepIndex < 0
      || !walkthrough
      || stored.stepIndex >= walkthrough.steps.length
      || typeof stored.updatedAt !== 'number'
      || !Number.isFinite(stored.updatedAt)
      || stored.updatedAt > now
      || now - stored.updatedAt > ACTIVE_RUN_MAX_AGE_MS
    ) {
      window.localStorage.removeItem(key);
      return null;
    }
    return {
      walkthrough,
      stepIndex: stored.stepIndex,
      projectId,
      updatedAt: stored.updatedAt,
    };
  } catch {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // Storage remains unavailable; there is nothing else to recover.
    }
    return null;
  }
}

export function WalkthroughProvider({
  children,
  projectId,
  walkthroughBadges = [],
}: {
  children: ReactNode;
  projectId?: string;
  walkthroughBadges?: readonly { id: string; badges: readonly string[] }[];
}) {
  const { walkthroughs } = useEditionModule();
  const [run, setRun] = useState<WalkthroughRun | null>(() => {
    const stored = restoreStoredRun(projectId, walkthroughs);
    return stored ? { id: 0, ...stored } : null;
  });
  const [walkthroughVisible, setWalkthroughVisible] = useState(false);
  // The chooser belongs to the project that opened it. Keeping that key in
  // state lets a child workspace bridge request the sample chooser during the
  // same commit as a project change without the recovery reset closing it.
  const [chooser, setChooser] = useState<{ projectId?: string } | null>(null);
  const [guideSeen, setGuideSeen] = useState(storedGuideSeen);
  const [sampleProjectId, setSampleProjectId] = useState<string | undefined>();
  const [introOpen, setIntroOpen] = useState(false);
  const [guideHintVisible, setGuideHintVisible] = useState(false);
  const nextRunId = useRef(1);
  const restoredProjectId = useRef(projectId);
  const visibleRun = run
    && (!run.projectId || run.projectId === projectId)
    && walkthroughs.some(
    (walkthrough) => walkthrough.id === run.walkthrough.id,
  ) ? run : null;
  const chooserOpen = chooser !== null && chooser.projectId === projectId;
  const sampleProjectActive = sampleProjectId !== undefined && sampleProjectId === projectId;
  const sampleIntroOpen = sampleProjectActive && introOpen;
  const sampleGuideHintVisible = sampleProjectActive && guideHintVisible;

  useEffect(() => {
    if (restoredProjectId.current === projectId) return;
    restoredProjectId.current = projectId;
    const stored = restoreStoredRun(projectId, walkthroughs);
    setChooser((current) => current?.projectId === projectId ? current : null);
    setWalkthroughVisible(false);
    setRun(stored ? { id: nextRunId.current++, ...stored } : null);
  }, [projectId, walkthroughs]);

  useEffect(() => {
    if (run) storeRun(run);
  }, [run]);

  const stopWalkthrough = useCallback(() => {
    clearStoredRun(run?.projectId);
    setRun(null);
    setWalkthroughVisible(false);
  }, [run?.projectId]);
  const pauseWalkthrough = useCallback(() => {
    setRun((current) => current ? { ...current, updatedAt: Date.now() } : null);
    setWalkthroughVisible(false);
  }, []);
  const resumeWalkthrough = useCallback(() => {
    if (!visibleRun) return;
    setRun({ ...visibleRun, updatedAt: Date.now() });
    setChooser(null);
    setWalkthroughVisible(true);
  }, [visibleRun]);
  const openWalkthroughChooser = useCallback(() => {
    setGuideSeen(true);
    storeGuideSeen();
    setGuideHintVisible(false);
    setWalkthroughVisible(false);
    setChooser({ projectId });
  }, [projectId]);
  const startWalkthrough = useCallback((id: string) => {
    const next = walkthroughs.find((candidate) => candidate.id === id);
    if (!next) return;
    setGuideSeen(true);
    storeGuideSeen();
    setGuideHintVisible(false);
    setChooser(null);
    setRun({
      id: nextRunId.current++,
      walkthrough: next,
      stepIndex: 0,
      projectId,
      updatedAt: Date.now(),
    });
    setWalkthroughVisible(true);
  }, [projectId, walkthroughs]);
  const enterSampleProject = useCallback((options: { openGuide?: boolean } = {}) => {
    setSampleProjectId(projectId);
    setWalkthroughVisible(false);
    if (options.openGuide) {
      storeFlag(SAMPLE_INTRO_SEEN_STORAGE_KEY);
      setIntroOpen(false);
      setGuideHintVisible(false);
      setGuideSeen(true);
      storeGuideSeen();
      setChooser({ projectId });
      return;
    }
    setChooser(null);
    if (storedFlag(SAMPLE_INTRO_SEEN_STORAGE_KEY)) {
      setIntroOpen(false);
      setGuideHintVisible(!guideSeen && !storedFlag(SAMPLE_HINT_DISMISSED_STORAGE_KEY));
      return;
    }
    setIntroOpen(true);
    setGuideHintVisible(false);
  }, [guideSeen, projectId]);
  const pokeAround = useCallback(() => {
    storeFlag(SAMPLE_INTRO_SEEN_STORAGE_KEY);
    setIntroOpen(false);
    setGuideHintVisible(!guideSeen && !storedFlag(SAMPLE_HINT_DISMISSED_STORAGE_KEY));
  }, [guideSeen]);
  const dismissGuideHint = useCallback(() => {
    storeFlag(SAMPLE_HINT_DISMISSED_STORAGE_KEY);
    setGuideHintVisible(false);
  }, []);
  const setStepIndex = useCallback((runId: number, stepIndex: number) => {
    setRun((current) => current?.id === runId
      ? { ...current, stepIndex, updatedAt: Date.now() }
      : current);
  }, []);
  const value = useMemo<WalkthroughContextValue>(() => ({
    active: sampleIntroOpen || chooserOpen || (visibleRun !== null && walkthroughVisible),
    canResume: visibleRun !== null && !walkthroughVisible,
    guideSeen,
    introOpen: sampleIntroOpen,
    guideHintVisible: sampleGuideHintVisible,
    guideEmphasized: sampleProjectActive && !guideSeen,
    openWalkthroughChooser,
    enterSampleProject,
    dismissGuideHint,
    resumeWalkthrough,
    startWalkthrough,
    stopWalkthrough,
  }), [
    chooserOpen,
    dismissGuideHint,
    enterSampleProject,
    guideSeen,
    sampleGuideHintVisible,
    sampleIntroOpen,
    openWalkthroughChooser,
    resumeWalkthrough,
    visibleRun,
    startWalkthrough,
    stopWalkthrough,
    walkthroughVisible,
    sampleProjectActive,
  ]);

  return (
    <WalkthroughContext.Provider value={value}>
      {children}
      {chooserOpen && (
        <WalkthroughChooser
          walkthroughs={walkthroughs}
          walkthroughBadges={walkthroughBadges}
          onStart={startWalkthrough}
          onClose={() => setChooser(null)}
        />
      )}
      {sampleIntroOpen && (
        <SampleProjectIntro
          walkthroughs={walkthroughs}
          walkthroughBadges={walkthroughBadges}
          onStart={(id) => {
            storeFlag(SAMPLE_INTRO_SEEN_STORAGE_KEY);
            setIntroOpen(false);
            startWalkthrough(id);
          }}
          onPokeAround={pokeAround}
        />
      )}
      {visibleRun && walkthroughVisible && (
        <WalkthroughHost
          key={visibleRun.id}
          walkthrough={visibleRun.walkthrough}
          stepIndex={visibleRun.stepIndex}
          onStepIndexChange={(stepIndex) => setStepIndex(visibleRun.id, stepIndex)}
          onPause={pauseWalkthrough}
          onFinish={stopWalkthrough}
        />
      )}
    </WalkthroughContext.Provider>
  );
}
