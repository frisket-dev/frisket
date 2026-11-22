import '../resolve/resolve.css';
import '../ActionPanel.css';
import { useEffect, useMemo } from 'react';
import { Button } from '../Button';
import { FormField } from '../FormControls';
import { PanelSelect } from '../PanelSelect';
import { MultiColumnPicker } from '../MultiColumnPicker';
import { ToggleRow } from '../PanelPrimitives';
import { sheetRequired, type SheetBoundParamsBodyProps } from './sheetRequiredParamsBody';
import { useEditorProblem } from './useEditorProblem';
import { useJoinSheets } from './useJoinSheets';

export const SpatialJoinParamsBody = sheetRequired(SpatialJoinParamsBodyForSheet);

function SpatialJoinParamsBodyForSheet({
  params,
  setParams,
  setEditorProblem,
  sheet,
  Field,
}: SheetBoundParamsBodyProps<'derive.spatial_join'>) {
  const { sheets, loading, error, refresh } = useJoinSheets(sheet);
  const blocked = loading || Boolean(error);
  const left = sheets.find((item) => item.id === sheet.id) ?? sheet;
  const pointColumns = useMemo(
    () => left.columns.filter((column) => column.type === 'geo_point'),
    [left.columns],
  );
  const sortedRightSheets = useMemo(() => {
    const otherSheets = sheets.filter((item) => item.id !== sheet.id);
    return [...otherSheets].sort((a, b) => {
      const aHasShape = a.columns.some((column) => column.type === 'geo_shape') ? 0 : 1;
      const bHasShape = b.columns.some((column) => column.type === 'geo_shape') ? 0 : 1;
      return aHasShape - bHasShape;
    });
  }, [sheets, sheet.id]);
  const right = sheets.find((item) => Number(item.id) === params.right_sheet?.sheet_id);
  const shapeColumns = useMemo(
    () => (right?.columns ?? []).filter((column) => column.type === 'geo_shape'),
    [right],
  );

  useEffect(() => {
    if (loading || error) return;
    const nextPoint = params.point_column || pointColumns[0]?.name || '';
    const defaultTarget = sortedRightSheets.find((item) =>
      item.columns.some((column) => column.type === 'geo_shape'),
    ) ?? sortedRightSheets[0];
    const nextRightId = params.right_sheet?.sheet_id ?? (defaultTarget ? Number(defaultTarget.id) : undefined);
    const resolvedRight = sheets.find((item) => Number(item.id) === nextRightId);
    const resolvedShapes = (resolvedRight?.columns ?? []).filter((column) => column.type === 'geo_shape');
    const nextShape = params.shape_column || (
      params.right_sheet?.sheet_id == null ? (resolvedShapes[0]?.name ?? '') : ''
    );

    const pointChanged = nextPoint !== (params.point_column ?? '');
    const rightChanged = nextRightId != null && nextRightId !== params.right_sheet?.sheet_id;
    const shapeChanged = nextShape !== (params.shape_column ?? '');

    if (pointChanged || rightChanged || shapeChanged) {
      setParams({
        ...params,
        ...(nextPoint ? { point_column: nextPoint } : {}),
        ...(nextRightId != null ? { right_sheet: { sheet_id: nextRightId } } : {}),
        ...(nextShape ? { shape_column: nextShape } : {}),
      });
    }
  }, [loading, error, params, setParams, sheets, pointColumns, sortedRightSheets]);

  const pointValid = Boolean(
    params.point_column && pointColumns.some((column) => column.name === params.point_column),
  );
  const shapeValid = Boolean(
    params.shape_column && shapeColumns.some((column) => column.name === params.shape_column),
  );
  const problem = loading
    ? 'Loading spatial join sheets…'
    : error
      ?? (pointColumns.length === 0
        ? 'This sheet has no geo_point column.'
        : !pointValid
          ? 'Choose a geo_point column.'
          : !right
            ? 'Choose a polygon sheet.'
            : shapeColumns.length === 0
              ? 'Selected sheet has no geo_shape column.'
              : !shapeValid
                ? 'Choose a geo_shape polygon column.'
                : null);

  useEditorProblem(setEditorProblem, problem);

  return (
    <div className="form-stack" data-testid="spatial-join-form">
      <Button
        type="button"
        className="btn-ghost"
        data-testid="spatial-join-refresh-sheets"
        onClick={refresh}
      >
        {error ? 'Retry' : 'Refresh sheets'}
      </Button>
      {loading && <p className="form-hint" role="status">Loading sheets…</p>}
      {error && <p className="panel-error" role="alert">{error}</p>}
      <fieldset className="resolve-drawer-body-fields" disabled={blocked}>
        <p className="form-hint">
          Points from <strong>{left.name}</strong> are matched against polygons in the target sheet.
        </p>

        <FormField
          label="Point column"
          hint={pointColumns.length === 0
            ? 'Add or geocode a geo_point column on this sheet first.'
            : 'geo_point column on the current sheet.'}
        >
          <PanelSelect
            testId="field-spatial_join_point_column"
            ariaLabel="Point column"
            value={params.point_column ?? ''}
            disabled={blocked || pointColumns.length === 0}
            options={[
              ...(!pointValid
                ? [{ value: params.point_column ?? '', label: params.point_column || 'Choose a geo_point column' }]
                : []),
              ...pointColumns.map((column) => ({ value: column.name, label: column.name })),
            ]}
            onValueChange={(point_column) => setParams({ ...params, point_column })}
          />
        </FormField>

        <FormField
          label="Polygon sheet"
          hint="Target sheet containing geo_shape polygons (GeoJSON or KML)."
        >
          <PanelSelect
            testId="field-spatial_join_right_sheet"
            ariaLabel="Polygon sheet"
            value={right?.id ?? ''}
            disabled={blocked}
            options={[
              ...(!right
                ? [{
                  value: '',
                  label: params.right_sheet?.sheet_id != null ? 'Saved sheet unavailable' : 'Choose a polygon sheet',
                }]
                : []),
              ...sortedRightSheets.map((item) => {
                const hasShape = item.columns.some((column) => column.type === 'geo_shape');
                return {
                  value: item.id,
                  label: hasShape ? item.name : `${item.name} (no geo_shape)`,
                };
              }),
            ]}
            onValueChange={(id) => {
              const numericId = Number(id);
              if (numericId === params.right_sheet?.sheet_id) return;
              const nextSheet = sheets.find((item) => Number(item.id) === numericId);
              const firstShape = nextSheet?.columns.find((column) => column.type === 'geo_shape')?.name ?? '';
              setParams({
                ...params,
                right_sheet: { sheet_id: numericId },
                shape_column: firstShape,
                right_columns: null,
              });
            }}
          />
        </FormField>

        <FormField
          label="Polygon column"
          hint={right && shapeColumns.length === 0
            ? 'Selected sheet has no geo_shape column.'
            : 'geo_shape column on the polygon sheet.'}
        >
          <PanelSelect
            testId="field-spatial_join_shape_column"
            ariaLabel="Polygon column"
            value={params.shape_column ?? ''}
            disabled={blocked || !right || shapeColumns.length === 0}
            options={[
              ...(!shapeValid
                ? [{ value: params.shape_column ?? '', label: params.shape_column || 'Choose a geo_shape column' }]
                : []),
              ...shapeColumns.map((column) => ({ value: column.name, label: column.name })),
            ]}
            onValueChange={(shape_column) => setParams({ ...params, shape_column })}
          />
        </FormField>

        <div className="form-row-pair">
          <FormField label="Join mode">
            <PanelSelect
              testId="field-spatial_join_how"
              ariaLabel="Join mode"
              value={params.how ?? 'left'}
              disabled={blocked}
              options={[
                { value: 'left', label: 'Left (keep all points)' },
                { value: 'inner', label: 'Inner (matched only)' },
              ]}
              onValueChange={(how) => setParams({ ...params, how: how as 'left' | 'inner' })}
            />
          </FormField>

          <FormField label="Match mode">
            <PanelSelect
              testId="field-spatial_join_match"
              ariaLabel="Match mode"
              value={params.match ?? 'all'}
              disabled={blocked}
              options={[
                { value: 'all', label: 'All matching polygons' },
                { value: 'first', label: 'First matching polygon' },
              ]}
              onValueChange={(match) => setParams({ ...params, match: match as 'all' | 'first' })}
            />
          </FormField>
        </div>

        <div className="form-field">
          <span className="form-field-label">Polygon columns to copy</span>
          <MultiColumnPicker
            testId="field-spatial_join_right_columns"
            ariaLabel="Polygon columns to copy"
            value={params.right_columns ?? []}
            options={(right?.columns ?? []).map(({ name, type, ai }) => ({
              name,
              type,
              ai: Boolean(ai),
            }))}
            disabled={blocked || !right}
            onValueChange={(names) => setParams({
              ...params,
              right_columns: names.length > 0 ? names : null,
            })}
          />
          <p className="form-hint">
            Leave empty to copy all columns from the polygon sheet. Colliding names get a <code>_right</code> suffix.
          </p>
        </div>

        <ToggleRow
          title="Include match indicator column"
          description="Adds a _merge column ('both' or 'left_only') showing whether each point matched a polygon."
          checked={params.indicator ?? false}
          testId="field-spatial_join_indicator"
          onCheckedChange={(indicator) => setParams({ ...params, indicator })}
        />

        <details className="action-advanced">
          <summary>Advanced options</summary>
          <Field name="max_output_rows" label="Maximum output rows" />
        </details>
      </fieldset>
    </div>
  );
}
