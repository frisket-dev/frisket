// Pure availability rules for the five work-view kinds. No React, no store
// access — the caller passes snapshots in.
//
// The per-kind rules live one-file-per-kind under core/selectors/views/*.view.ts,
// discovered via import.meta.glob (see views/registry.ts). This file's PUBLIC API
// (selectWorkViewAvailability, the WorkView* types, the memoization behavior
// workView.test.ts pins) is UNCHANGED by that — a new work-view kind is a new
// views/<kind>.view.ts file, not an edit to this function.

import type { ColumnDef, SheetMeta } from '../../api/open';
import { memoizeByArgs } from './memo';
import { WORK_VIEW_DESCRIPTORS } from './views/registry';

export type WorkViewKind = 'grid' | 'document' | 'map' | 'gallery' | 'graph' | 'answers';

export interface WorkViewAvailabilityInput {
  sheet: SheetMeta | null;
  /** DATA, not a closure. From useWorkbenchContributionVisibility. */
  /** Sourced from the NEW `hiddenContributionIdSet` field, not the hook's
   *  existing `hiddenContributionIds: string[]`. */
  hiddenContributionIds: ReadonlySet<string>;
  /** Resolved from the runtime index host-side; null = no enabled map plugin. */
  mapContributionId: string | null;
  /** Descriptor ids used by the availability rules. NOTE: no `imageGallery`
   *  id here — the gallery segment intentionally does not consult
   *  hidden-ness; only `graphNeighborhood` is needed. */
  contributionIds: {
    graphNeighborhood: string;
  };
  /** Pure column-shape probes lifted verbatim from the current inline code. */
  firstGeoColumn: ColumnDef | null; // useWorkspaceModel.tsx
  firstMediaColumn: ColumnDef | null; // useWorkspaceModel.tsx (documentMediaColumns)
  imageGalleryAvailable: boolean; // resolvePluginViewAvailability(...).available
  sheetIsEdgeShaped: boolean; // materializedKind edge|join
  /** DISTINCT active-evidence column ids for the sheet, straight from
   *  `SheetMeta.citedColumnIds` — always present (possibly []), stable
   *  reference across renders while `sheet` itself doesn't change. */
  citedColumnIds: readonly string[];
  /** DISTINCT annotated SOURCE-text column ids, straight from
   *  `SheetMeta.annotatedTextColumnIds` — always present (possibly []).
   *  Deliberately NOT `citedColumnIds`: the Document view's text source has to
   *  be gated on the narrow signal or it lights up on every sheet that carries
   *  any evidence at all (annotated-text-layers R13). */
  annotatedTextColumnIds: readonly string[];
}

export type WorkViewStatus = 'enabled' | 'disabled';
export interface WorkViewEntry {
  available: boolean;
  status: WorkViewStatus;
  /** Machine reason; UI copy is mapped in the region, not here. */
  reason: 'available' | 'missing_contribution' | 'hidden_by_profile' | 'data_requirements_unmet';
}
export type WorkViewAvailability = Record<WorkViewKind, WorkViewEntry>;

function computeWorkViewAvailability(
  sheet: SheetMeta | null,
  hiddenContributionIds: ReadonlySet<string>,
  mapContributionId: string | null,
  graphNeighborhoodContributionId: string,
  firstGeoColumn: ColumnDef | null,
  firstMediaColumn: ColumnDef | null,
  imageGalleryAvailable: boolean,
  sheetIsEdgeShaped: boolean,
  citedColumnIds: readonly string[],
  annotatedTextColumnIds: readonly string[],
): WorkViewAvailability {
  // Reassembled into the shared WorkViewAvailabilityInput shape each
  // glob-registered view descriptor's computeEntry expects (views/*.view.ts)
  // — the individual primitive args above are what memoizeByArgs keys on
  // (object-identity-based caching over a freshly-allocated object per call
  // would never hit).
  const input: WorkViewAvailabilityInput = {
    sheet,
    hiddenContributionIds,
    mapContributionId,
    contributionIds: { graphNeighborhood: graphNeighborhoodContributionId },
    firstGeoColumn,
    firstMediaColumn,
    imageGalleryAvailable,
    sheetIsEdgeShaped,
    citedColumnIds,
    annotatedTextColumnIds,
  };
  // NO galleryHidden check — this is INTENTIONAL parity with today's
  // behavior, not an oversight; pinned in gallery.view.ts's own header
  // comment, not restated per-call here. Map and graph DO check hidden-ness
  // (their own view files); this inconsistency is recorded, not silently
  // fixed.
  const result = {} as WorkViewAvailability;
  for (const descriptor of Object.values(WORK_VIEW_DESCRIPTORS)) {
    result[descriptor.kind] = descriptor.computeEntry(input);
  }
  return result;
}

// Single-entry cache keyed on the primitive/reference args extracted from the
// input record (core/selectors/memo.ts) — avoids reallocating the
// availability record on every render when nothing the rules depend on has
// changed.
const memoizedCompute = memoizeByArgs(computeWorkViewAvailability);

export function selectWorkViewAvailability(
  i: WorkViewAvailabilityInput,
): WorkViewAvailability {
  return memoizedCompute(
    i.sheet,
    i.hiddenContributionIds,
    i.mapContributionId,
    i.contributionIds.graphNeighborhood,
    i.firstGeoColumn,
    i.firstMediaColumn,
    i.imageGalleryAvailable,
    i.sheetIsEdgeShaped,
    i.citedColumnIds,
    i.annotatedTextColumnIds,
  );
}
