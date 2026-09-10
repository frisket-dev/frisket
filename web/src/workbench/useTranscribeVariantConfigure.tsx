// The Transcribe Compare variant-configure + add-engine state machine. Owns the
// add-engine menu (immediate-add flow with its inline remote-gate), the per-chip
// Configure-variant popover (edit-only — the gear never seeds a blank column,
// unlike OCR), the viewport-shift clamp, dismissal, focus, and the native player
// source peek.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import type { TranscribeCompareEngineResult } from '../api/open';
import { horizontalViewportShift, useAnchoredPosition, type AnchoredPosition } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';
import { MediaPlayerPeek } from './MediaPlayerPeek';
import type {
  MediaCompareSession,
  MediaNavTarget,
  ScratchDoc,
} from './mediaCompareSession';

export function useTranscribeVariantConfigure(
  session: MediaCompareSession<TranscribeCompareEngineResult>,
) {
  const {
    catalog,
    columns,
    addEngineColumn,
    updateColumnOptions,
    chooseEngine,
    duplicateColumn,
  } = session;

  const [addMenuOpen, setAddMenuOpen] = useState(false);
  // addMenuRef: the wrapper (trigger + popover); addMenuTriggerRef: the
  // trigger button alone, for top-layer placement; addMenuPopRef: the
  // popover element itself, for the native popover lifecycle.
  const addMenuRef = useRef<HTMLDivElement>(null);
  const addMenuTriggerRef = useRef<HTMLButtonElement>(null);
  const addMenuPopRef = useRef<HTMLDivElement>(null);

  // Configure-variant popover: which chip's ⚙ is open. Unlike OCR this never
  // seeds a blank "adding" column — the add-menu (below) stays the add
  // flow; the gear only EDITS an already-committed chip's engine/options.
  const [configureVariantId, setConfigureVariantId] = useState<string | null>(null);
  const configureAnchorRef = useRef<HTMLDivElement>(null);
  const engineSelectRef = useRef<HTMLSelectElement>(null);
  const configurePopoverRef = useRef<HTMLDivElement>(null);

  // Active engine ids (chips) and the engines still available to add (menu).
  const activeEngineIds = useMemo(
    () => new Set(columns.map((column) => column.engineId).filter((id): id is string => !!id)),
    [columns],
  );
  const addableEngines = useMemo(
    () => catalog.filter((engine) => !activeEngineIds.has(engine.id)),
    [catalog, activeEngineIds],
  );

  // Add-menu dismissal: a native top-layer popover, `escape:false` is
  // deliberate — outside pointerdown only, no Escape (the menu is a plain
  // add-engine list, not a form worth a keyboard-cancel affordance). The
  // trigger testid is the ignoreSelector so its own click keeps sole
  // ownership of the toggle.
  const dismissAddMenu = useCallback(() => {
    setAddMenuOpen(false);
  }, []);
  useNativePopover(addMenuPopRef, dismissAddMenu, {
    enabled: addMenuOpen,
    escape: false,
    ignoreSelector: '[data-testid="transcribe-compare-add-engine"]',
  });
  // Fixed-position anchor now that the menu is a top-layer element — it no
  // longer inherits placement from `.ocr-compare-add-wrap`'s CSS positioned
  // ancestor (the shared `.menu-pop`, right-aligned under the trigger).
  const addMenuPos: AnchoredPosition | null = useAnchoredPosition(addMenuTriggerRef, {
    enabled: addMenuOpen,
    align: 'right',
    width: 224,
    gap: 4,
  });

  // Configure popover dismissal — Escape or an outside pointerdown closes it
  // (parity with OcrCompareTab's dismissConfigure; no "incomplete add" case
  // here since the gear never seeds a blank column). Nothing to cancel on
  // dismiss any more: every engine the picker offers is free, so a swap is
  // always immediately runnable and can never be stranded mid-consent.
  const dismissConfigure = useCallback(() => {
    if (!configureVariantId) return;
    setConfigureVariantId(null);
  }, [configureVariantId]);
  // Inline Escape + outside-pointerdown: excluded from the top-layer
  // conversion, same lighter genus as the OCR configure popover —
  // inline-absolute, positioned via the horizontalViewportShift JS clamp
  // only, no portal. `useDismissable` retired without a shared successor
  // for this genus, so the two-listener contract it provided (default
  // escape:true/outside:true) is ported verbatim, inline.
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

  // Viewport clamp — same horizontalViewportShift primitive OcrCompareTab
  // uses for this popover genus (see its longer comment); recomputed on open
  // and on resize while a popover is open.
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

  const clearEngineOptions = useCallback((variantId: string) => {
    // updateColumnOptions is a merge API; explicit undefined values retire the
    // previous engine's settings while preserving that generic session seam.
    // Every consumer projects by runtime type, so none of these keys can leak
    // into a request after the swap.
    updateColumnOptions(variantId, {
      language: undefined,
      modelSize: undefined,
      vad: undefined,
      diarize: undefined,
      numSpeakers: undefined,
      minSpeakers: undefined,
      maxSpeakers: undefined,
    });
  }, [updateColumnOptions]);

  const onConfigureChooseEngine = useCallback(
    (variantId: string, engineId: string) => {
      chooseEngine(variantId, engineId);
      clearEngineOptions(variantId);
    },
    [chooseEngine, clearEngineOptions],
  );

  const onConfigureDuplicate = useCallback(
    (variantId: string) => {
      const copyId = duplicateColumn(variantId);
      // Open the copy's popover (the small-vs-large model-size flow).
      if (copyId) setConfigureVariantId(copyId);
    },
    [duplicateColumn],
  );

  const onSelectEngine = useCallback(
    (engineId: string) => {
      // No billable confirm interposed in the menu: the menu only ever lists
      // free engines, so picking one adds it straight away.
      addEngineColumn(engineId);
      setAddMenuOpen(false);
    },
    [addEngineColumn],
  );

  const renderSourcePeek = useCallback(
    (doc: ScratchDoc<TranscribeCompareEngineResult>, nav: MediaNavTarget | null): ReactNode => {
      const seekSeconds = nav?.kind === 'seconds' ? nav.seconds : null;
      return (
        <MediaPlayerPeek
          url={doc.objectUrl}
          mediaKind={doc.mediaKind === 'video' ? 'video' : 'audio'}
          seekSeconds={seekSeconds}
          testidPrefix="transcribe-compare"
        />
      );
    },
    [],
  );

  return {
    configureVariantId,
    setConfigureVariantId,
    popoverShift,
    configureAnchorRef,
    engineSelectRef,
    configurePopoverRef,
    openConfigure,
    onConfigureChooseEngine,
    onConfigureDuplicate,
    addMenuOpen,
    setAddMenuOpen,
    addMenuRef,
    addMenuTriggerRef,
    addMenuPopRef,
    addMenuPos,
    addableEngines,
    onSelectEngine,
    renderSourcePeek,
  };
}
