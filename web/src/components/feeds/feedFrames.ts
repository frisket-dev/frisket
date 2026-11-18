import type { ReactNode } from 'react';
import type { KnownFeedKind } from './feedConfig';

type FeedDetailFrame = ({ children }: { children: ReactNode }) => ReactNode;
export type FeedKindFormFrame = ({ sourceKind, children }: {
  sourceKind: KnownFeedKind;
  children: ReactNode;
}) => ReactNode;

export interface FeedDetailFrames {
  Summary: FeedDetailFrame;
  Health: FeedDetailFrame;
  Runs: FeedDetailFrame;
}
