import type { ComponentType } from 'react';
import type { ActionBodyProps } from '../../generated/actionUI';

import type {
  CanonicalActionDraft,
} from '../../actions/canonicalActionDraft';
import type { GeneratedActionDraft, SheetMeta } from '../../api/open';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import type { EngineOption } from '../../api/types';

interface GeneratedActionBodyContext {
  sheet: SheetMeta | null;
  /** Served catalog facts used by engine-specific option controls. */
  engine?: Readonly<EngineOption>;
  /** Read-only authored request context; naming and scope setters stay in the host. */
  request: Readonly<Pick<GeneratedActionDraft, 'scope' | 'sheet_name' | 'output_names'>>;
  onNavigateToAction?(actionKind: string, sourceColumn?: string): void;
  sampleColumnValues?(columnId: string): string[];
}

export type GeneratedActionParamsBodyProps<K extends keyof GeneratedActionParams> =
  ActionBodyProps<GeneratedActionParams[K], GeneratedActionBodyContext>;

/** A custom action may own only its coordinated Params editor. The generated
 * host keeps the drawer, output naming, preview/run, cost, and lifecycle. */
export type GeneratedActionParamsBody<K extends keyof GeneratedActionParams> =
  ComponentType<GeneratedActionParamsBodyProps<K>>;

/** The catalog id is runtime data at the host boundary. Exact action/body
 * association is checked before storage; only lookup erases it to this shape. */
export type DynamicGeneratedActionParamsBody = ComponentType<
  ActionBodyProps<CanonicalActionDraft, GeneratedActionBodyContext>>;
