import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import type { GeneratedActionFieldProps } from './generatedActionCustomizations';
import { PanelSelect } from '../PanelSelect';

export function CensusGeographyField({ id, testid, label, value, onChange }: GeneratedActionFieldProps) {
  return <div className="param-row">
    <label className="form-label" htmlFor={id}>{label}</label>
    <PanelSelect id={id} testId={testid} value={typeof value === 'string' ? value : 'tract'}
      onValueChange={onChange} options={[
        { value: 'tract', label: 'Census tract' },
        { value: 'block_group', label: 'Census block group' },
      ]} />
  </div>;
}

export function GeocodeParamsBody({ params, setParams, Field }:
  GeneratedActionParamsBodyProps<'enrich.geocode'>) {
  return <>
    <Field name="source" label="Address" />
    <p className="form-hint" data-testid="action-input-summary">
      Use an address column or compose an address from multiple columns.
    </p>
    <Field name="engine" />
    <p className="form-hint">Auto uses OpenCage when OPENCAGE_API_KEY is configured, else Nominatim.</p>
    <div className="action-io-summary">
      <div className="form-label">Outputs</div>
      <div className="form-hint" data-testid="action-output-summary">
        <strong>geo_point</strong> is the map column; <strong>formatted_address</strong>{' '}
        is the geocoder-normalized address.
        {params.include_lat_lon && ' latitude and longitude duplicate the point as scalar columns.'}
      </div>
      <label className="form-check">
        <input type="checkbox" data-testid="include-lat-lon-columns"
          checked={params.include_lat_lon ?? false}
          onChange={(event) => setParams({ ...params, include_lat_lon: event.target.checked })} />
        Also include latitude and longitude columns
      </label>
    </div>
  </>;
}

export function CensusParamsBody({ sheet, Field, onNavigateToAction }:
  GeneratedActionParamsBodyProps<'enrich.census_demographics'>) {
  return <>
    {(sheet?.columns ?? []).every((column) => column.type !== 'geo_point') && (
      <div className="empty-inline" data-testid="census-no-geo-point">
        <span>No compatible <code>geo_point</code> columns.</span>
        <button type="button" className="mini-btn"
          onClick={() => onNavigateToAction?.('enrich.geocode')}>Run Geocode first</button>
      </div>
    )}
    <Field name="source" label="Geo point column" />
    <Field name="geography" />
    <Field name="include_moe" />
  </>;
}
