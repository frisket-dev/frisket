import type { ReactNode, Ref, RefObject } from 'react';

/** Presentation state projected by the selector catalog. It deliberately does
 * not encode how a caller authors an engine/model value. */
export type EngineSelectorStatus = 'ready' | 'needs_setup' | 'working' | 'unavailable';

export interface EngineSelectorFact {
  label: string;
  value: string | number | readonly string[];
}

export interface EngineSelectorChoice {
  /** Stable catalog identity. Keep opaque provider-qualified IDs intact. */
  id: string;
  label: string;
  summary?: string;
  description?: string;
  facts?: readonly EngineSelectorFact[];
  destination?: string;
  modelCardUrl?: string;
  status: EngineSelectorStatus;
  /** Whether this choice may become a new authored selection. */
  canAuthor: boolean;
  /** Preflight display only; execution admission remains outside this component. */
  canRun?: boolean;
  blocker?: string;
  isDefault?: boolean;
}

export interface EngineSelectorGroup {
  id: string;
  label: string;
  choices: readonly EngineSelectorChoice[];
}

export interface EngineSelectorDetailFooterContext {
  choice: EngineSelectorChoice;
  /** A setup-needed selection remains pinned until explicit navigation. */
  pinned: boolean;
  /** The supplied form may focus this region after it mounts. */
  regionRef: RefObject<HTMLDivElement | null>;
  /** Call when an embedded form starts/stops editing. */
  onEditingChange(editing: boolean): void;
}

export interface EngineSelectorProps {
  label: string;
  groups: readonly EngineSelectorGroup[];
  /** Current authored catalog choice; this is never changed by hover. */
  value: string | null;
  /** Browser-storage boundary, normally workspace + surface/action context. */
  recentNamespace: string;
  onSelect(choice: EngineSelectorChoice): void;
  renderDetailFooter?(context: EngineSelectorDetailFooterContext): ReactNode;
  disabled?: boolean;
  searchPlaceholder?: string;
  /** Optional focus target for host validation flows. */
  triggerRef?: Ref<HTMLButtonElement>;
}
