import { useRef, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';
import { topLayerPortalRoot } from '../topLayerPortal';
import styles from './GuideHint.module.css';

export function GuideHint({ anchorRef, onOpenGuide, onDismiss }: {
  anchorRef: RefObject<HTMLButtonElement | null>;
  onOpenGuide(): void;
  onDismiss(): void;
}) {
  const hintRef = useRef<HTMLDivElement>(null);
  const position = useAnchoredPosition(anchorRef, {
    enabled: true,
    width: (_rect, viewportWidth) => Math.min(320, viewportWidth - 24),
    gap: 8,
    align: 'right',
    minHeight: 100,
  });
  useNativePopover(hintRef, onDismiss, {
    outside: false,
    ignoreSelector: '[data-testid="chrome-walkthrough"]',
  });

  return createPortal(
    <div ref={hintRef} className={styles.hint} style={position ?? undefined}
      role="dialog" aria-modal="false" aria-label="Try a walkthrough" data-testid="sample-guide-hint">
      <p id="sample-guide-hint-text">
        Not sure where to start? Walkthroughs highlight the buttons to press as you explore the sample data.
      </p>
      <div className={styles.actions}>
        <button type="button" className="btn btn-primary" onClick={onOpenGuide}>Open Guide →</button>
        <button type="button" className="btn" onClick={onDismiss}>Got it</button>
      </div>
    </div>,
    topLayerPortalRoot(),
  );
}
