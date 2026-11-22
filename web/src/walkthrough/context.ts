import { createContext, useContext, useEffect, useLayoutEffect, useRef } from 'react';
import type { WalkthroughFillSpec } from './walkthroughs';

export type WalkthroughFillerFn = (spec: WalkthroughFillSpec) => boolean;

export interface WalkthroughContextValue {
  active: boolean;
  canResume: boolean;
  guideSeen: boolean;
  /** Whether the sample project's first-open choice is currently displayed. */
  introOpen: boolean;
  /** Whether the one-time sample Guide nudge should be rendered by Chrome. */
  guideHintVisible: boolean;
  /** Whether Chrome should draw attention to Guide for the active sample project. */
  guideEmphasized: boolean;
  openWalkthroughChooser(): void;
  enterSampleProject(options?: { openGuide?: boolean }): void;
  dismissGuideHint(): void;
  resumeWalkthrough(): void;
  startWalkthrough(id: string): void;
  stopWalkthrough(): void;
  /** Register a direct React state filler for a mounted form; returns an unregister cleanup. */
  registerWalkthroughFiller(id: string, filler: WalkthroughFillerFn): () => void;
}

export const WalkthroughContext = createContext<WalkthroughContextValue | null>(null);

export function useWalkthrough(): WalkthroughContextValue {
  const value = useContext(WalkthroughContext);
  if (!value) throw new Error('useWalkthrough must be used inside WalkthroughProvider');
  return value;
}

export function useOptionalWalkthrough(): WalkthroughContextValue | null {
  return useContext(WalkthroughContext);
}

/** Register a ref-backed walkthrough filler without causing parent re-renders. */
export function useWalkthroughFiller(id: string | null, filler: WalkthroughFillerFn): void {
  const walkthrough = useOptionalWalkthrough();
  const register = walkthrough?.registerWalkthroughFiller;
  const fillerRef = useRef(filler);
  useLayoutEffect(() => {
    fillerRef.current = filler;
  });
  useEffect(() => {
    if (!register || !id) return undefined;
    return register(id, (spec) => fillerRef.current(spec));
  }, [register, id]);
}
