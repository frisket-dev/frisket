// grid-in-place-edit-v1: double-click/Enter on an eligible grid cell now edits
// it in place (glide's own overlay) instead of opening the row drawer.
//
// Two separate checks compose this feature, pinned in the two describe
// blocks below:
//  - cellEditability(col) — COLUMN-level eligibility. Post-review retreat:
//    text renderer ONLY (number/integer/stars/boolean were dropped — see the
//    doc comment on EDITABLE_RENDERERS in cells.tsx for the three concrete
//    reasons: single-click boolean toggle, empty-marker type erasure to a
//    string, and glide's Number editor not enforcing integer/stars range).
//  - isOverlayEditable(cell) — the per-CELL check activation actually
//    branches on: a landed error, a pending/incomplete/withheld placeholder,
//    or a preview cell is never overlay-editable even in an eligible column,
//    so activation must fall through to the drawer for those instead of
//    going dead (no editor, no drawer).
// shouldOpenDrawerOnKey composes the per-cell check with the actual key
// (Space always drawer; Enter drawer only when NOT overlay-editable).
import { GridCellKind } from '@glideapps/glide-data-grid';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  buildCell,
  cellEditability,
  isOverlayEditable,
  oneClickFacetValue,
  shouldOpenDrawerOnKey,
  withOneClickFacetCursor,
} from '../../src/grid/cells';
import { applyColumnTypeRegistry, facetBehaviorFor } from '../../src/grid/typeRegistry';
import { aiMeta, columnDef, row } from '../support/domainFixtures';

describe('cellEditability', () => {
  it('is editable for a plain (non-AI, non-markdown) text column', () => {
    expect(cellEditability(columnDef({ type: 'text' }))).toBe(true);
  });

  it('is NOT editable for number/integer/stars/boolean columns (post-review retreat)', () => {
    expect(cellEditability(columnDef({ type: 'number' }))).toBe(false);
    expect(cellEditability(columnDef({ type: 'integer' }))).toBe(false);
    expect(cellEditability(columnDef({ type: 'boolean' }))).toBe(false);
    // 'stars' has no core registry entry (it's plugin-registered with
    // renderer: 'stars' in fixtures), but the renderer name alone is enough
    // to prove it's excluded from EDITABLE_RENDERERS regardless of the
    // column's declared type.
    expect(cellEditability(columnDef({ type: 'stars' }))).toBe(false);
  });

  it('is never editable for an AI-generated column, even a text one', () => {
    expect(cellEditability(columnDef({ type: 'text', ai: aiMeta() }))).toBe(false);
  });

  it('is not editable for a markdown-formatted text column (native preview-on-open stays)', () => {
    expect(cellEditability(columnDef({ type: 'text', format: 'markdown' }))).toBe(false);
  });

  it('is not editable for structurally non-text renderers (json/media/link/geo/category/image)', () => {
    expect(cellEditability(columnDef({ type: 'json' }))).toBe(false);
    expect(cellEditability(columnDef({ type: 'link' }))).toBe(false);
    expect(cellEditability(columnDef({ type: 'image' }))).toBe(false);
    expect(cellEditability(columnDef({ type: 'video' }))).toBe(false);
    expect(cellEditability(columnDef({ type: 'geo_point' }))).toBe(false);
    expect(cellEditability(columnDef({ type: 'category' }))).toBe(false);
  });

  it('is not editable for an unregistered/unknown column type', () => {
    expect(cellEditability(columnDef({ type: 'totally-unknown-type' }))).toBe(false);
  });
});

describe('category facet behavior', () => {
  beforeEach(() => {
    applyColumnTypeRegistry([
      {
        name: 'category',
        core: true,
        presentation: {
          renderer: 'category',
          facet: { kind: 'categorical', preferred: true, oneClick: true, operator: 'eq' },
        },
        hasValidator: true,
        hasParser: false,
        description: 'One label from a small set.',
      },
      {
        name: 'boolean',
        core: true,
        presentation: {
          renderer: 'boolean',
          facet: { kind: 'categorical', preferred: true, oneClick: true, operator: 'eq' },
        },
        hasValidator: true,
        hasParser: false,
        description: 'True or false.',
      },
    ]);
  });

  it('is declared by the type registry rather than inferred from the bubble renderer', () => {
    expect(facetBehaviorFor('category')).toEqual({
      kind: 'categorical',
      preferred: true,
      oneClick: true,
      operator: 'eq',
    });
    expect(facetBehaviorFor('text')?.kind).toBe('categorical');
  });

  it('does not infer facet behavior from the category renderer', () => {
    applyColumnTypeRegistry([
      {
        name: 'plugin_category',
        core: false,
        presentation: { renderer: 'category' },
        hasValidator: true,
        hasParser: false,
        description: 'Plugin-owned category-like renderer without facet semantics.',
      },
    ]);
    expect(facetBehaviorFor('plugin_category')).toBeNull();
  });

  it('rejects malformed and unsupported plugin facet metadata as a closed contract', () => {
    const plugin = (name: string, facet: Record<string, unknown>) => ({
      name,
      core: false,
      presentation: { renderer: 'text', facet },
      hasValidator: true,
      hasParser: false,
      description: name,
    });
    applyColumnTypeRegistry([
      plugin('bad_operator', { kind: 'categorical', operator: 'starts_with' }),
      plugin('bad_range', { kind: 'range', valueKind: 'currency' }),
      plugin('extra_key', { kind: 'range', valueKind: 'number', operator: 'gte' }),
    ]);
    expect(facetBehaviorFor('bad_operator')).toBeNull();
    expect(facetBehaviorFor('bad_range')).toBeNull();
    expect(facetBehaviorFor('extra_key')).toBeNull();
  });

  it('turns a real category value into the exact filter declared by the registry', () => {
    const col = columnDef({ type: 'category' });
    expect(oneClickFacetValue(col, row({ [col.id]: 'active' }))).toEqual({
      value: 'active',
      operator: 'eq',
    });
  });

  it('does not make blank or synthetic status bubbles clickable filters', () => {
    const col = columnDef({ type: 'category' });
    expect(oneClickFacetValue(col, row({ [col.id]: '' }))).toBeNull();
    expect(
      oneClickFacetValue(
        col,
        row({ [col.id]: 'active' }, { cellErrors: { [col.id]: 'failed' } }),
      ),
    ).toBeNull();
    expect(oneClickFacetValue(columnDef({ type: 'text' }), row({ [col.id]: 'active' })))
      .toBeNull();
  });

  it('shows a pointer only when the exact one-click facet value is actionable', () => {
    const category = columnDef({ type: 'category' });
    const boolean = columnDef({ id: 'flag', type: 'boolean' });
    const real = row({ [category.id]: 'active' });
    const falseValue = row({ flag: false });
    const categoryCell = buildCell(category, real);

    expect(withOneClickFacetCursor(categoryCell, category, real)).toMatchObject({
      cursor: 'pointer',
    });
    expect(withOneClickFacetCursor(buildCell(boolean, falseValue), boolean, falseValue))
      .toMatchObject({ cursor: 'pointer' });

    const nonFacets = [
      row({ [category.id]: '' }),
      row({ [category.id]: 'active' }, { cellErrors: { [category.id]: 'failed' } }),
      row({ [category.id]: 'active' }, { cellOutcomes: { [category.id]: 'withheld_unverified' } }),
      row({ [category.id]: 'active' }, { cellStates: { [category.id]: 'incomplete' } }),
    ];
    for (const nonFacet of nonFacets) {
      expect(withOneClickFacetCursor(buildCell(category, nonFacet), category, nonFacet))
        .not.toHaveProperty('cursor');
    }
  });
});

describe('buildCell editable overlay override', () => {
  it('flips a normal text cell to allowOverlay+writable when editable', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(col, row({ [col.id]: 'hello' }), { editable: cellEditability(col) });
    expect(cell.kind).toBe(GridCellKind.Text);
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(true);
    expect((cell as { readonly?: boolean }).readonly).toBe(false);
  });

  it('leaves the cell read-only when editable is not passed (e.g. the preview overlay path)', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(col, row({ [col.id]: 'hello' }));
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(false);
  });

  it('flips a genuinely-empty text cell too (spreadsheet convention: click an empty cell, type)', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(col, row({ [col.id]: null }), { editable: true });
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(true);
    expect((cell as { readonly?: boolean }).readonly).toBe(false);
  });

  it('does not flip a Bubble placeholder (withheld) even when editable is true', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(
      col,
      row({ [col.id]: null }, { cellOutcomes: { [col.id]: 'withheld_unverified' } }),
      { editable: true },
    );
    expect(cell.kind).toBe(GridCellKind.Bubble);
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(false);
  });

  it('never flips a number/boolean cell even if a caller mistakenly passes editable: true', () => {
    // cellEditability(col) is the real gate SheetGrid uses (and it now always
    // returns false for these types), but withEditableOverlay's own kind
    // check is a second line of defense: it only ever flips Text-kind cells.
    const numberCol = columnDef({ type: 'number' });
    expect((buildCell(numberCol, row({ [numberCol.id]: 3 }), { editable: true }) as {
      allowOverlay?: boolean;
    }).allowOverlay).toBe(false);
    const boolCol = columnDef({ type: 'boolean' });
    expect((buildCell(boolCol, row({ [boolCol.id]: true }), { editable: true }) as {
      allowOverlay?: boolean;
    }).allowOverlay).toBe(false);
  });

  it('does not flip an error cell even in an editable text column (fix 2: activation must still reach the drawer)', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(
      col,
      row({ [col.id]: null }, { cellErrors: { [col.id]: 'boom' } }),
      { editable: true },
    );
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(false);
  });
});

describe('buildCell markdown wrapping', () => {
  it('uses a read-only wrapping text cell when the global wrap mode is on', () => {
    const col = columnDef({ type: 'text', format: 'markdown', ai: aiMeta() });
    const cell = buildCell(col, row({ [col.id]: 'A long generated answer' }), { wrap: true });
    expect(cell.kind).toBe(GridCellKind.Text);
    expect((cell as { allowWrapping?: boolean }).allowWrapping).toBe(true);
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(false);
    expect((cell as { readonly?: boolean }).readonly).toBe(true);
  });

  it('keeps the rendered markdown preview when wrap mode is off', () => {
    const col = columnDef({ type: 'text', format: 'markdown', ai: aiMeta() });
    const cell = buildCell(col, row({ [col.id]: '# Generated answer' }), { wrap: false });
    expect(cell.kind).toBe(GridCellKind.Markdown);
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(true);
  });
});

describe('isOverlayEditable (the per-cell activation check)', () => {
  it('is true for a normal editable text cell', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(col, row({ [col.id]: 'hello' }), { editable: cellEditability(col) });
    expect(isOverlayEditable(cell)).toBe(true);
  });

  it('is false for a plain (non-editable) text cell', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(col, row({ [col.id]: 'hello' }));
    expect(isOverlayEditable(cell)).toBe(false);
  });

  it('is false for a landed error cell in an otherwise-editable text column', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(
      col,
      row({ [col.id]: null }, { cellErrors: { [col.id]: 'boom' } }),
      { editable: cellEditability(col) },
    );
    expect(isOverlayEditable(cell)).toBe(false);
  });

  it('is false for a pending (streaming-fill) cell', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(col, row({ [col.id]: null }), {
      editable: cellEditability(col),
      pending: true,
    });
    expect(cell.kind).toBe(GridCellKind.Custom);
    expect(isOverlayEditable(cell)).toBe(false);
  });

  it('is false for an incomplete-state Bubble placeholder', () => {
    const col = columnDef({ type: 'text' });
    const cell = buildCell(
      col,
      row({ [col.id]: null }, { cellStates: { [col.id]: 'incomplete' } }),
      { editable: cellEditability(col) },
    );
    expect(cell.kind).toBe(GridCellKind.Bubble);
    expect(isOverlayEditable(cell)).toBe(false);
  });

  it('is false for a markdown preview cell (allowOverlay true, but readonly — a preview, not an editor)', () => {
    const col = columnDef({ type: 'text', format: 'markdown' });
    const cell = buildCell(col, row({ [col.id]: '# heading' }), { editable: cellEditability(col) });
    expect((cell as { allowOverlay?: boolean }).allowOverlay).toBe(true);
    expect((cell as { readonly?: boolean }).readonly).toBe(true);
    expect(isOverlayEditable(cell)).toBe(false);
  });
});

describe('shouldOpenDrawerOnKey', () => {
  it('Space always opens the drawer, editable cell or not', () => {
    expect(shouldOpenDrawerOnKey(' ', true)).toBe(true);
    expect(shouldOpenDrawerOnKey(' ', false)).toBe(true);
  });

  it('Enter opens the drawer only when the cell is NOT overlay-editable', () => {
    expect(shouldOpenDrawerOnKey('Enter', false)).toBe(true);
    expect(shouldOpenDrawerOnKey('Enter', true)).toBe(false);
  });

  it('any other key never opens the drawer', () => {
    expect(shouldOpenDrawerOnKey('a', false)).toBe(false);
    expect(shouldOpenDrawerOnKey('Escape', false)).toBe(false);
    expect(shouldOpenDrawerOnKey('ArrowRight', true)).toBe(false);
  });
});
