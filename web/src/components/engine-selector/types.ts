import type { ReactNode, Ref } from 'react';

/** Presentation state projected by the selector catalog. It deliberately does
 * not encode how a caller authors an engine/model value. */
export type EngineSelectorStatus = 'ready' | 'needs_setup' | 'working' | 'unavailable';

export interface EngineSelectorFact {
  label: string;
  value: string | number | readonly string[];
}

/** The one execution route resolved by the server for this project/model. */
export interface EngineSelectorRoute {
  label: string;
  billedTo?: string;
  credentialLabel?: string;
  sendsDataTo?: string;
  balanceLabel?: string;
  balanceUrl?: string;
  settingsUrl?: string;
  hasAlternativeRoute?: boolean;
}

/** A caller-owned operation projected into the selected field's status copy. */
export interface EngineSelectorActivity {
  label: string;
  /** `null` means the operation is active but has no measurable completion. */
  percent: number | null;
}

export interface EngineSelectorChoice {
  /** Stable catalog identity. Keep opaque provider-qualified IDs intact. */
  id: string;
  label: string;
  summary?: string;
  description?: string;
  facts?: readonly EngineSelectorFact[];
  route?: EngineSelectorRoute;
  destination?: string;
  modelCardUrl?: string;
  status: EngineSelectorStatus;
  /** Whether this choice may become a new authored selection. */
  canAuthor: boolean;
  /** Preflight display only; execution admission remains outside this component. */
  canRun?: boolean;
  blocker?: string;
  /** Active setup/download progress supplied by the catalog lifecycle. */
  activity?: EngineSelectorActivity;
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
  /** Call when an embedded form starts/stops editing. */
  onEditingChange(editing: boolean): void;
  /** Dismiss the selector after a custom detail action completes. */
  close(): void;
}

export interface EngineSelectorProps {
  label: string;
  groups: readonly EngineSelectorGroup[];
  /** Current authored catalog choice; this is never changed by hover. */
  value: string | null;
  /** Browser-storage boundary, normally workspace + surface/action context. */
  recentNamespace: string;
  onSelect(choice: EngineSelectorChoice): void;
  /** Caller-owned controls that sit between static facts and the resolved
   * route. Invoked only for the one previewed choice. */
  renderDetailBeforeRoute?(context: EngineSelectorDetailFooterContext): ReactNode;
  renderDetailFooter?(context: EngineSelectorDetailFooterContext): ReactNode;
  /** Caller-owned status or recovery content, kept inside the modal while open. */
  notice?: ReactNode;
  disabled?: boolean;
  searchPlaceholder?: string;
  /** Optional focus target for host validation flows. */
  triggerRef?: Ref<HTMLButtonElement>;
}
