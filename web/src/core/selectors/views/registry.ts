// Glob self-registration for the five work-view kinds. Each kind lives in its
// own core/selectors/views/<kind>.view.ts file, default-exporting a
// WorkViewDescriptor (types.ts). This barrel discovers them via
// `import.meta.glob('./*.view.ts', { eager: true })` and asserts uniqueness
// (core/registration/glob.ts's collectEagerRegistrations) — a duplicate `kind`
// throws at module load instead of silently overwriting.
//
// Retires two formerly hand-maintained inventories:
//   - `WORK_VIEW_TITLES` (was a `Record<Exclude<WorkViewKind,'grid'>, string>`
//     literal) — now derived from each descriptor's own `title` field below.
//   - the switcher's `workViewSegments` kind list (was a hand-typed
//     `{grid, document, map, gallery, graph}` object) — useWorkspaceModel.tsx
//     now derives the KIND LIST (which views exist) from
//     `Object.keys(WORK_VIEW_DESCRIPTORS)` instead of a second hand-typed
//     literal; the boolean VALUES still come from workViewAvailability
//     (selectWorkViewAvailability's output), unchanged.
//
// Framework-free (core/ boundary).

import { collectEagerRegistrations } from '../../registration/glob';
import type { WorkViewKind } from '../workView';
import type { WorkViewDescriptor } from './types';

const viewModules = import.meta.glob('./*.view.ts', { eager: true }) as Record<
  string,
  { default: WorkViewDescriptor }
>;

// Completeness against the WorkViewKind union moves to test-time here too
// (same as commands, registry.ts's header comment) —
// core/registration/viewRegistration.test.ts asserts every kind is present
// and, per kind !== 'grid', carries a title.
export const WORK_VIEW_DESCRIPTORS = collectEagerRegistrations(
  viewModules,
  (descriptor) => descriptor.kind,
  'work-view',
) as Record<WorkViewKind, WorkViewDescriptor>;

export const WORK_VIEW_KINDS = Object.keys(WORK_VIEW_DESCRIPTORS) as WorkViewKind[];

export const WORK_VIEW_TITLES: Partial<Record<WorkViewKind, string>> = Object.fromEntries(
  Object.values(WORK_VIEW_DESCRIPTORS)
    .filter((descriptor): descriptor is WorkViewDescriptor & { title: string } =>
      descriptor.title !== undefined,
    )
    .map((descriptor) => [descriptor.kind, descriptor.title]),
);
