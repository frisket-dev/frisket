// The Transcribe Compare variant-configure state machine owns the shared
// selector-backed add/configure flow, its dismissal and focus, and the native
// player source peek.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import type { TranscribeCompareEngineResult } from '../api/open';
import { horizontalViewportShift } from '../hooks/useAnchoredPosition';
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
    columns,
    addBlankColumn,
    removeColumn,
    updateColumnOptions,
    chooseEngine,
    duplicateColumn,
  } = session;

  // Configure-variant popover: which chip's selector is open.
  const [configureVariantId, setConfigureVariantId] = useState<string | null>(null);
  const [addingVariantId, setAddingVariantId] = useState<string | null>(null);
  const configureAnchorRef = useRef<HTMLDivElement>(null);
  const engineSelectorRef = useRef<HTMLButtonElement>(null);
  const configurePopoverRef = useRef<HTMLDivElement>(null);

  const dismissConfigure = useCallback(() => {
    if (!configureVariantId) return;
    const variant = columns.find((column) => column.id === configureVariantId);
    if (addingVariantId === configureVariantId && !variant?.engineId) {
      removeColumn(configureVariantId);
      setAddingVariantId(null);
    }
    setConfigureVariantId(null);
  }, [addingVariantId, columns, configureVariantId, removeColumn]);
  // Inline Escape + outside-pointerdown: excluded from the top-layer
  // conversion, same lighter genus as the OCR configure popover —
  // inline-absolute, positioned via the horizontalViewportShift JS clamp
  // only, no portal. `useDismissable` retired without a shared successor
  // for this genus, so the two-listener contract it provided (default
  // escape:true/outside:true) is ported verbatim, inline.
  useEffect(() => {
    if (!configureVariantId) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target instanceof Element && event.target.closest('.engine-selector__dialog')) return;
      if (event.key === 'Escape') dismissConfigure();
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (target instanceof Element && target.closest('.engine-selector__dialog')) return;
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
    engineSelectorRef.current?.focus();
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
      setAddingVariantId((current) => current === variantId ? null : current);
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

  const addVariant = useCallback(() => {
    const variantId = addBlankColumn();
    setAddingVariantId(variantId);
    setConfigureVariantId(variantId);
  }, [addBlankColumn]);

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
    engineSelectorRef,
    configurePopoverRef,
    openConfigure,
    onConfigureChooseEngine,
    onConfigureDuplicate,
    addVariant,
    renderSourcePeek,
  };
}
