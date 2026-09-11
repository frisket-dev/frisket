import { createContext, useContext } from 'react';

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
}

export const WalkthroughContext = createContext<WalkthroughContextValue | null>(null);

export function useWalkthrough(): WalkthroughContextValue {
  const value = useContext(WalkthroughContext);
  if (!value) throw new Error('useWalkthrough must be used inside WalkthroughProvider');
  return value;
}
