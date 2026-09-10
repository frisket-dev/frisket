// Truth-table parity pin for selectWorkViewAvailability. NOT done if any
// workViewSegments boolean flips on the truth table.

import { describe, expect, it } from 'vitest';
import type { ColumnDef } from '../../api/open';
import { selectWorkViewAvailability, type WorkViewAvailabilityInput } from './workView';

const GEO_COLUMN: ColumnDef = { id: 'geo-1', name: 'Location', type: 'geo_point' };
const MEDIA_COLUMN: ColumnDef = { id: 'media-1', name: 'File', type: 'file' };
const GRAPH_NEIGHBORHOOD_ID = 'graphNeighborhood';
const MAP_CONTRIBUTION_ID = 'mapPlugin';

function baseInput(overrides: Partial<WorkViewAvailabilityInput> = {}): WorkViewAvailabilityInput {
  return {
    sheet: null,
    hiddenContributionIds: new Set<string>(),
    mapContributionId: MAP_CONTRIBUTION_ID,
    contributionIds: { graphNeighborhood: GRAPH_NEIGHBORHOOD_ID },
    firstGeoColumn: GEO_COLUMN,
    firstMediaColumn: MEDIA_COLUMN,
    imageGalleryAvailable: true,
    sheetIsEdgeShaped: true,
    citedColumnIds: ['col-answer-1'],
    annotatedTextColumnIds: [],
    ...overrides,
  };
}

describe('selectWorkViewAvailability', () => {
  // ---- grid: always available (useWorkspaceModel.tsx, `grid: true`) -------
  it('grid is always available regardless of every other input', () => {
    const empty = selectWorkViewAvailability(
      baseInput({
        mapContributionId: null,
        firstGeoColumn: null,
        firstMediaColumn: null,
        imageGalleryAvailable: false,
        sheetIsEdgeShaped: false,
        hiddenContributionIds: new Set([MAP_CONTRIBUTION_ID, GRAPH_NEIGHBORHOOD_ID]),
      }),
    );
    expect(empty.grid).toEqual({ available: true, status: 'enabled', reason: 'available' });
  });

  // ---- document: firstMediaColumn !== null OR annotatedTextColumnIds -------
  describe('document', () => {
    it('available when a media column exists', () => {
      const result = selectWorkViewAvailability(baseInput());
      expect(result.document).toEqual({ available: true, status: 'enabled', reason: 'available' });
    });

    it('data_requirements_unmet when there is neither media nor annotated text', () => {
      const result = selectWorkViewAvailability(baseInput({ firstMediaColumn: null }));
      expect(result.document).toEqual({
        available: false,
        status: 'disabled',
        reason: 'data_requirements_unmet',
      });
    });

    // The annotated-text reader's entry point. A paste/text-only sheet has no
    // media column at all, so before this it offered only Grid and Answers and
    // its extracted entities could never be read in context.
    it('available on a media-less sheet once a text column carries annotations', () => {
      const result = selectWorkViewAvailability(
        baseInput({ firstMediaColumn: null, annotatedTextColumnIds: ['col-body'] }),
      );
      expect(result.document).toEqual({ available: true, status: 'enabled', reason: 'available' });
    });

    // The narrow signal, not "has a text column": citedColumnIds names NER's
    // OUTPUT column and every other evidence kind, so gating on it would offer
    // the view on every sheet with any evidence at all.
    it('does NOT read citedColumnIds as an annotated-text signal', () => {
      const result = selectWorkViewAvailability(
        baseInput({
          firstMediaColumn: null,
          citedColumnIds: ['col-answer-1', 'col-entities'],
          annotatedTextColumnIds: [],
        }),
      );
      expect(result.document.available).toBe(false);
    });
  });

  // ---- map: mapContributionId / hidden / firstGeoColumn --------------------
  describe('map', () => {
    it('missing_contribution when no map plugin is enabled, regardless of hidden/geo state', () => {
      const result = selectWorkViewAvailability(
        baseInput({
          mapContributionId: null,
          hiddenContributionIds: new Set([MAP_CONTRIBUTION_ID]), // irrelevant when id is null
          firstGeoColumn: null,
        }),
      );
      expect(result.map).toEqual({
        available: false,
        status: 'disabled',
        reason: 'missing_contribution',
      });
    });

    it('hidden_by_profile when the map contribution is hidden, regardless of geo column', () => {
      const result = selectWorkViewAvailability(
        baseInput({ hiddenContributionIds: new Set([MAP_CONTRIBUTION_ID]), firstGeoColumn: null }),
      );
      expect(result.map).toEqual({
        available: false,
        status: 'disabled',
        reason: 'hidden_by_profile',
      });
    });

    it('data_requirements_unmet when the contribution is present and visible but no geo column', () => {
      const result = selectWorkViewAvailability(baseInput({ firstGeoColumn: null }));
      expect(result.map).toEqual({
        available: false,
        status: 'disabled',
        reason: 'data_requirements_unmet',
      });
    });

    it('available when the contribution is present, visible, and a geo column exists', () => {
      const result = selectWorkViewAvailability(baseInput());
      expect(result.map).toEqual({ available: true, status: 'enabled', reason: 'available' });
    });
  });

  // ---- gallery: imageGalleryAvailable only — NO hidden-id check ------------
  // (asymmetry with the switcher's map/graph is INTENTIONAL and pinned here,
  // not "fixed"; see workView.ts's header comment.)
  describe('gallery', () => {
    it('available when the plugin resolves it available', () => {
      const result = selectWorkViewAvailability(baseInput({ imageGalleryAvailable: true }));
      expect(result.gallery).toEqual({ available: true, status: 'enabled', reason: 'available' });
    });

    it('data_requirements_unmet when the plugin resolves it unavailable', () => {
      const result = selectWorkViewAvailability(baseInput({ imageGalleryAvailable: false }));
      expect(result.gallery).toEqual({
        available: false,
        status: 'disabled',
        reason: 'data_requirements_unmet',
      });
    });

    it('stays available even when its own contribution id is in the hidden set', () => {
      const result = selectWorkViewAvailability(
        baseInput({
          imageGalleryAvailable: true,
          hiddenContributionIds: new Set(['imageGallery', MAP_CONTRIBUTION_ID, GRAPH_NEIGHBORHOOD_ID]),
        }),
      );
      expect(result.gallery).toEqual({ available: true, status: 'enabled', reason: 'available' });
    });
  });

  // ---- graph: graphNeighborhoodHidden / sheetIsEdgeShaped -------------------
  describe('graph', () => {
    it('data_requirements_unmet when the sheet is not edge-shaped and the contribution is visible', () => {
      const result = selectWorkViewAvailability(baseInput({ sheetIsEdgeShaped: false }));
      expect(result.graph).toEqual({
        available: false,
        status: 'disabled',
        reason: 'data_requirements_unmet',
      });
    });

    it('hidden_by_profile when the sheet is not edge-shaped and the contribution is hidden', () => {
      const result = selectWorkViewAvailability(
        baseInput({ sheetIsEdgeShaped: false, hiddenContributionIds: new Set([GRAPH_NEIGHBORHOOD_ID]) }),
      );
      expect(result.graph).toEqual({
        available: false,
        status: 'disabled',
        reason: 'hidden_by_profile',
      });
    });

    it('available when the sheet is edge-shaped and the contribution is visible', () => {
      const result = selectWorkViewAvailability(baseInput({ sheetIsEdgeShaped: true }));
      expect(result.graph).toEqual({ available: true, status: 'enabled', reason: 'available' });
    });

    it('hidden_by_profile when the sheet is edge-shaped and the contribution is hidden', () => {
      const result = selectWorkViewAvailability(
        baseInput({ sheetIsEdgeShaped: true, hiddenContributionIds: new Set([GRAPH_NEIGHBORHOOD_ID]) }),
      );
      expect(result.graph).toEqual({
        available: false,
        status: 'disabled',
        reason: 'hidden_by_profile',
      });
    });
  });

  // ---- answers: citedColumnIds.length > 0 — a plain non-empty check, no
  // contribution/hidden-ness concept (there is no plugin behind this view;
  // mirrors document's data-keyed shape). ------------------------------
  describe('answers', () => {
    it('available when the sheet carries at least one cited column', () => {
      const result = selectWorkViewAvailability(baseInput({ citedColumnIds: ['col-1'] }));
      expect(result.answers).toEqual({ available: true, status: 'enabled', reason: 'available' });
    });

    it('data_requirements_unmet when the sheet carries zero cited columns', () => {
      const result = selectWorkViewAvailability(baseInput({ citedColumnIds: [] }));
      expect(result.answers).toEqual({
        available: false,
        status: 'disabled',
        reason: 'data_requirements_unmet',
      });
    });
  });

  // ---- Combined scenarios mirroring workViewSegments -----------------------
  it('everything available: matches the all-enabled workViewSegments row', () => {
    const result = selectWorkViewAvailability(baseInput());
    expect({
      grid: result.grid.available,
      document: result.document.available,
      map: result.map.available,
      gallery: result.gallery.available,
      graph: result.graph.available,
    }).toEqual({ grid: true, document: true, map: true, gallery: true, graph: true });
  });

  it('everything unavailable except grid and gallery: matches an all-hidden/no-data workViewSegments row', () => {
    const result = selectWorkViewAvailability(
      baseInput({
        mapContributionId: null,
        firstGeoColumn: null,
        firstMediaColumn: null,
        imageGalleryAvailable: true, // gallery ignores hidden-ness; this is its own independent axis
        sheetIsEdgeShaped: false,
        hiddenContributionIds: new Set([GRAPH_NEIGHBORHOOD_ID]),
      }),
    );
    expect({
      grid: result.grid.available,
      document: result.document.available,
      map: result.map.available,
      gallery: result.gallery.available,
      graph: result.graph.available,
    }).toEqual({ grid: true, document: false, map: false, gallery: true, graph: false });
  });

  // ---- Memoization sanity: stable input references return the same object -
  it('returns a cached result object when called twice with reference-equal inputs', () => {
    const input = baseInput();
    const first = selectWorkViewAvailability(input);
    const second = selectWorkViewAvailability(input);
    expect(second).toBe(first);
  });

  it('recomputes when a referenced input (e.g. firstGeoColumn) changes identity', () => {
    const first = selectWorkViewAvailability(baseInput());
    const second = selectWorkViewAvailability(
      baseInput({ firstGeoColumn: { ...GEO_COLUMN } }),
    );
    expect(second).not.toBe(first);
    expect(second).toEqual(first); // same values, new object
  });
});
