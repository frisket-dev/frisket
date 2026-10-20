import type { ReactNode } from 'react';
import type { KnownSourceKind } from './sourceConfig';

type SourceDetailFrame = ({ children }: { children: ReactNode }) => ReactNode;
export type SourceKindFormFrame = ({ sourceKind, children }: {
  sourceKind: KnownSourceKind;
  children: ReactNode;
}) => ReactNode;

export interface SourceDetailFrames {
  Summary: SourceDetailFrame;
  Health: SourceDetailFrame;
  Runs: SourceDetailFrame;
}
