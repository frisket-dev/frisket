// The OCR Compare variant-configure state machine. Owns the Configure-variant
// popover open/add/edit flow (the popover IS the add flow), the
// viewport-shift clamp, dismissal, and the pdf.js source-peek renderer.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import type { OcrComparePreviewPageResult } from '../api/open';
import { horizontalViewportShift } from '../hooks/useAnchoredPosition';
import { DocumentReader } from './DocumentReader';
import type {
  MediaCompareSession,
  MediaNavTarget,
  ScratchDoc,
} from './mediaCompareSession';

export function useOcrVariantConfigure(
  session: MediaCompareSession<OcrComparePreviewPageResult[]>,
) {
  const {
    columns,
    addBlankColumn,
    chooseEngine,
    duplicateColumn,
    removeColumn,
  } = session;

  // Configure-variant popover: which variant is open (null = closed). The
  // popover IS the add flow, the edit flow, and the error-reason surface.
  const [configureVariantId, setConfigureVariantId] = useState<string | null>(null);
  // The variant seeded by ＋add (blank "Choose engine…"); dismissing without a
  // chosen engine removes it. Cleared once an engine is committed.
  const [addingVariantId, setAddingVariantId] = useState<string | null>(null);
  const configureAnchorRef = useRef<HTMLDivElement>(null);
  const engineSelectRef = useRef<HTMLSelectElement>(null);
  const configurePopoverRef = useRef<HTMLDivElement>(null);
  const addingRef = useRef<string | null>(null);
  useEffect(() => {
    addingRef.current = addingVariantId;
  }, [addingVariantId]);

  // Viewport clamp: the popover always
  // opens left-anchored under its chip; a chip near the right edge of the
  // viewport (or the row's own scroll container) would otherwise render the
  // popover partly off-screen. Shift it left by exactly the overflow amount
  // — never more, so it stays flush against the anchor whenever there's
  // room. Recomputed on open and on resize/scroll while a popover is open.
  //
  // This deliberately stays on the horizontalViewportShift primitive rather
  // than the full useAnchoredPosition hook: the popover is NOT portaled and
  // NOT `position: fixed` measured off the anchor rect — it renders inline
  // inside `.ocr-compare-variant` at `position: absolute; top: calc(100% +
  // 6px); left: 0` (styles.css `.ocr-compare-configure`) and only needs a
  // supplemental translateX nudge when that natural placement would overflow
  // the viewport. useAnchoredPosition's top/left/maxHeight resolution assumes
  // a portaled, viewport-relative menu (see its own doc comment); forcing
  // that model here would mean portaling this popover out of its chip's flow,
  // which is unnecessary for this shift-only popover.
  const [popoverShift, setPopoverShift] = useState(0);
  const popoverShiftRef = useRef(0);
  useLayoutEffect(() => {
    if (!configureVariantId) {
      if (popoverShiftRef.current !== 0) {
        popoverShiftRef.current = 0;
        setPopoverShift(0);
      }
      return undefined;
    }
    const clamp = () => {
      const node = configurePopoverRef.current;
      if (!node) return;
      const shift = horizontalViewportShift(node.getBoundingClientRect(), popoverShiftRef.current);
      if (Math.abs(popoverShiftRef.current - shift) > 0.5) {
        popoverShiftRef.current = shift;
        setPopoverShift(shift);
      }
    };
    clamp();
    window.addEventListener('resize', clamp);
    return () => window.removeEventListener('resize', clamp);
  }, [configureVariantId]);

  const openConfigure = useCallback((variantId: string) => {
    setConfigureVariantId((prev) => (prev === variantId ? null : variantId));
  }, []);

  const addEngineFlow = useCallback(() => {
    const variantId = addBlankColumn();
    setConfigureVariantId(variantId);
    setAddingVariantId(variantId);
  }, [addBlankColumn]);

  const onChooseEngine = useCallback(
    (variantId: string, engineId: string) => {
      // Choosing a catalog engine commits its configuration. Any required
      // paid/external consent belongs to the batch Run gate, not this picker.
      chooseEngine(variantId, engineId);
      setAddingVariantId((prev) => (prev === variantId ? null : prev));
    },
    [chooseEngine],
  );

  const onRemoveVariant = useCallback(
    (variantId: string) => {
      removeColumn(variantId);
      setConfigureVariantId((prev) => (prev === variantId ? null : prev));
      setAddingVariantId((prev) => (prev === variantId ? null : prev));
    },
    [removeColumn],
  );

  const onDuplicate = useCallback(
    (variantId: string) => {
      const copyId = duplicateColumn(variantId);
      setAddingVariantId(null);
      // Open the copy's popover.
      if (copyId) setConfigureVariantId(copyId);
    },
    [duplicateColumn],
  );

  // Configure popover dismissal: Escape or an outside pointerdown closes it
  // (never on hover). An incomplete add (no engine chosen) is discarded.
  const dismissConfigure = useCallback(() => {
    const variantId = configureVariantId;
    if (!variantId) return;
    const variant = columns.find((candidate) => candidate.id === variantId);
    // Discard an incomplete add: no engine chosen yet. The old second arm
    // (a remote engine stranded at its billing gate) is gone with the gate —
    // a chosen engine is now always runnable.
    if (variant && variant.engineId === null) {
      onRemoveVariant(variantId);
    }
    setConfigureVariantId(null);
    setAddingVariantId(null);
  }, [configureVariantId, columns, onRemoveVariant]);
  // Inline Escape + outside-pointerdown: excluded from the top-layer
  // conversion — this popover is inline-absolute relative to its own chip,
  // not portaled/anchored, see the horizontalViewportShift comment above.
  // `useDismissable` retired without a shared successor for this genus, so
  // the two-listener contract it provided (default escape:true/outside:true,
  // no typingGuard/overlayAware/focusRestore/ignoreSelector/extraRefs) is
  // ported verbatim, inline.
  useEffect(() => {
    if (!configureVariantId) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismissConfigure();
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (configureAnchorRef.current?.contains(target)) return;
      dismissConfigure();
    };
    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.removeEventListener('pointerdown', onPointerDown, true);
    };
  }, [configureVariantId, dismissConfigure]);

  useEffect(() => {
    if (!configureVariantId) return;
    engineSelectRef.current?.focus();
  }, [configureVariantId]);

  const renderSourcePeek = useCallback(
    (doc: ScratchDoc<OcrComparePreviewPageResult[]>, navTarget: MediaNavTarget | null): ReactNode => {
      const page = navTarget?.kind === 'page' ? navTarget.page : 1;
      return (
        <DocumentReader
          key={`${doc.id}:${page}`}
          media={session.peekMedia}
          mediaKind={doc.mediaKind === 'image' ? 'image' : 'pdf'}
          title={doc.filename}
          layout="continuous"
          fit="width"
          videoFit="full"
          onVideoFitChange={() => undefined}
          textLayer
          onPageCount={() => undefined}
          rowKey={doc.id}
          onOpenDetail={() => undefined}
          canOpenDetail={false}
          optionsOpen={false}
          onToggleOptions={() => undefined}
          optionsPopover={null}
          selectionCount={0}
          initialPage={page}
        />
      );
    },
    [session.peekMedia],
  );

  return {
    configureVariantId,
    popoverShift,
    configureAnchorRef,
    engineSelectRef,
    configurePopoverRef,
    openConfigure,
    addEngineFlow,
    onChooseEngine,
    onRemoveVariant,
    onDuplicate,
    renderSourcePeek,
  };
}
