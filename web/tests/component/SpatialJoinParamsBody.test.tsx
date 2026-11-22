// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry, type ActionParamResolution } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { actionFormProjectApi } from '../support/renderActionForm';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

type Props = ComponentProps<typeof GeneratedActionForm>;

const catalog = servedActionCatalog();

const pointsSheet = sheetMeta([
  columnDef({ id: '11', name: 'incident', type: 'text' }),
  columnDef({ id: '12', name: 'location', type: 'geo_point' }),
  columnDef({ id: '13', name: 'alt_point', type: 'geo_point' }),
], { id: '1', name: 'Incidents', rowCount: 4 });

const plainSheet = sheetMeta([
  columnDef({ id: '21', name: 'label', type: 'text' }),
], { id: '2', name: 'Notes', rowCount: 2 });

const districtsSheet = sheetMeta([
  columnDef({ id: '31', name: 'district', type: 'text' }),
  columnDef({ id: '32', name: 'geometry', type: 'geo_shape' }),
  columnDef({ id: '33', name: 'buffer_geom', type: 'geo_shape' }),
], { id: '3', name: 'Districts', rowCount: 6 });

const spatialApi = {
  ...actionFormProjectApi,
  listSheets: vi.fn(async () => [pointsSheet, plainSheet, districtsSheet]),
};

const harness = createWorkspaceTestHarness({
  projectId: 'typed-spatial-join-form',
  api: { projectApi: spatialApi },
});

function renderSpatialJoinForm(props: Partial<Props> = {}) {
  const raw = catalog.actions.find((entry) => entry.kind === 'derive.spatial_join');
  if (!raw || !isGeneratedActionCatalogEntry(raw)) {
    throw new Error('Missing derive.spatial_join catalog entry');
  }
  const onExecute = vi.fn();
  const resolveParams = vi.fn<Props['resolveParams']>(async ({ params }): Promise<ActionParamResolution> => {
    const rightSheet = params.right_sheet as { sheet_id?: number } | undefined;
    const valid = Boolean(params.point_column && rightSheet?.sheet_id && params.shape_column);
    return {
      diagnostics: valid ? {} : { point_column: { ok: false, message: 'Complete spatial join inputs.' } },
      logical_outputs: valid
        ? [
          { key: 'incident', column_type: 'text' },
          { key: 'location', column_type: 'geo_point' },
          { key: 'district', column_type: 'text' },
          ...(params.indicator ? [{ key: '_merge', column_type: 'text' }] : []),
        ]
        : [],
    };
  });
  const base: Props = {
    catalogEntry: raw,
    actionTemplate: generatedActionTemplateFromCatalogEntry(raw)!,
    sheet: pointsSheet,
    running: false,
    onClose() {},
    resolveParams,
    onExecute,
    ...props,
  };
  return {
    ...harness.render(<GeneratedActionForm {...base} />),
    onExecute,
    resolveParams,
    entry: raw,
  };
}

afterEach(cleanup);
beforeEach(() => {
  spatialApi.listSheets.mockReset().mockResolvedValue([pointsSheet, plainSheet, districtsSheet]);
});

describe('SpatialJoinParamsBody', () => {
  it('auto-selects geo_point, geo_shape sheet & column, and submits configured spatial join request', async () => {
    const view = renderSpatialJoinForm();

    await waitFor(() => {
      expect(screen.getByTestId('generated-action-run')).toBeEnabled();
    });

    // Auto-defaults pick geo_point on left and the sheet containing geo_shape on right
    expect(screen.getByTestId('field-spatial_join_point_column')).toHaveValue('location');
    expect(screen.getByTestId('field-spatial_join_right_sheet')).toHaveValue('3');
    expect(screen.getByTestId('field-spatial_join_shape_column')).toHaveValue('geometry');
    expect(screen.getByTestId('field-spatial_join_how')).toHaveValue('left');
    expect(screen.getByTestId('field-spatial_join_match')).toHaveValue('all');

    // Change point column, shape column, how, match, and indicator
    fireEvent.change(screen.getByTestId('field-spatial_join_point_column'), {
      target: { value: 'alt_point' },
    });
    fireEvent.change(screen.getByTestId('field-spatial_join_shape_column'), {
      target: { value: 'buffer_geom' },
    });
    fireEvent.change(screen.getByTestId('field-spatial_join_how'), {
      target: { value: 'inner' },
    });
    fireEvent.change(screen.getByTestId('field-spatial_join_match'), {
      target: { value: 'first' },
    });
    fireEvent.click(screen.getByTestId('field-spatial_join_indicator'));

    await waitFor(() => {
      expect(screen.getByTestId('generated-action-run')).toBeEnabled();
    });

    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(view.onExecute).toHaveBeenCalledTimes(1);
    expect(view.onExecute).toHaveBeenCalledWith(
      expect.objectContaining({
        action_id: 'derive.spatial_join',
        scope: { kind: 'sheet_rows', sheet_id: 1 },
        sheet_name: 'Spatial joined',
        params: expect.objectContaining({
          point_column: 'alt_point',
          right_sheet: { sheet_id: 3 },
          shape_column: 'buffer_geom',
          how: 'inner',
          match: 'first',
          indicator: true,
        }),
      }),
      'run',
    );
  });

  it('blocks submission when selected polygon sheet has no geo_shape column', async () => {
    renderSpatialJoinForm();

    await waitFor(() => {
      expect(screen.getByTestId('generated-action-run')).toBeEnabled();
    });

    fireEvent.change(screen.getByTestId('field-spatial_join_right_sheet'), {
      target: { value: '2' },
    });

    await waitFor(() => {
      expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    });
    expect(screen.getByTestId('field-spatial_join_shape_column')).toBeDisabled();
  });

  it('blocks submission when current sheet has no geo_point column', async () => {
    spatialApi.listSheets.mockResolvedValue([plainSheet, districtsSheet]);
    renderSpatialJoinForm({ sheet: plainSheet });

    await waitFor(() => {
      expect(screen.getByTestId('field-spatial_join_point_column')).toBeDisabled();
    });
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });
});
