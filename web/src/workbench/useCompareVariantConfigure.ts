import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { horizontalViewportShift } from '../hooks/useAnchoredPosition';
import type { MediaCompareSession } from './mediaCompareSession';

type VariantEditorSession = Pick<MediaCompareSession<unknown>,
  'columns' | 'addBlankColumn' | 'chooseEngine' | 'duplicateColumn' | 'removeColumn'>;

/** Inline comparison editors share a lifecycle, not their engine option policy.
 * Only a newly-created, still-blank variant is abandoned when its editor closes. */
export function useCompareVariantConfigure(
  session: VariantEditorSession,
  resetEngineOptions?: (variantId: string) => void,
) {
  const { columns, addBlankColumn, chooseEngine, duplicateColumn, removeColumn } = session;
  const [configureVariantId, setConfigureVariantId] = useState<string | null>(null);
  const provisionalId = useRef<string | null>(null);
  const configureAnchorRef = useRef<HTMLDivElement>(null);
  const configurePopoverRef = useRef<HTMLDivElement>(null);
  const engineSelectorRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const discardProvisional = useCallback(() => {
    const id = provisionalId.current;
    provisionalId.current = null;
    if (id && columns.some((column) => column.id === id && !column.engineId)) removeColumn(id);
  }, [columns, removeColumn]);

  const dismissConfigure = useCallback(() => {
    discardProvisional();
    setConfigureVariantId(null);
  }, [discardProvisional]);

  const showConfigure = useCallback((variantId: string) => {
    if (variantId === configureVariantId) return;
    discardProvisional();
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    setConfigureVariantId(variantId);
  }, [configureVariantId, discardProvisional]);

  const openConfigure = useCallback((variantId: string) => {
    if (variantId === configureVariantId) dismissConfigure();
    else showConfigure(variantId);
  }, [configureVariantId, dismissConfigure, showConfigure]);

  const addVariant = useCallback(() => {
    discardProvisional();
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    const id = addBlankColumn();
    provisionalId.current = id;
    setConfigureVariantId(id);
  }, [addBlankColumn, discardProvisional]);

  const onChooseEngine = useCallback((variantId: string, engineId: string) => {
    chooseEngine(variantId, engineId);
    resetEngineOptions?.(variantId);
    if (provisionalId.current === variantId) provisionalId.current = null;
  }, [chooseEngine, resetEngineOptions]);

  const onRemoveVariant = useCallback((variantId: string) => {
    if (provisionalId.current === variantId) provisionalId.current = null;
    removeColumn(variantId);
    setConfigureVariantId((current) => current === variantId ? null : current);
  }, [removeColumn]);

  const onDuplicate = useCallback((variantId: string) => {
    const copyId = duplicateColumn(variantId);
    if (!copyId) return;
    discardProvisional();
    provisionalId.current = columns.find((column) => column.id === variantId)?.engineId
      ? null : copyId;
    setConfigureVariantId(copyId);
  }, [columns, discardProvisional, duplicateColumn]);

  useEffect(() => {
    if (configureVariantId) engineSelectorRef.current?.focus();
  }, [configureVariantId]);

  useEffect(() => {
    if (!configureVariantId) return undefined;
    const isNestedSelector = (target: EventTarget | null) => target instanceof Element
      && Boolean(target.closest('.engine-selector__dialog'));
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || isNestedSelector(event.target)) return;
      event.preventDefault();
      dismissConfigure();
      if (returnFocusRef.current?.isConnected) returnFocusRef.current.focus();
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || isNestedSelector(event.target)) return;
      if (configureAnchorRef.current?.contains(event.target)
        || configurePopoverRef.current?.contains(event.target)) return;
      dismissConfigure();
    };
    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.removeEventListener('pointerdown', onPointerDown, true);
    };
  }, [configureVariantId, dismissConfigure]);

  // These editors stay inline/absolute beneath their chip, not top-layer fixed
  // menus. Reuse the shared positioning primitive without changing their layout.
  const [popoverShift, setPopoverShift] = useState(0);
  const popoverShiftRef = useRef(0);
  useLayoutEffect(() => {
    if (!configureVariantId) return undefined;
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
    window.addEventListener('scroll', clamp, true);
    return () => {
      window.removeEventListener('resize', clamp);
      window.removeEventListener('scroll', clamp, true);
    };
  }, [configureVariantId]);

  return { configureVariantId, configureAnchorRef, configurePopoverRef, engineSelectorRef,
    popoverShift, openConfigure, showConfigure, addVariant, onChooseEngine, onRemoveVariant, onDuplicate };
}
