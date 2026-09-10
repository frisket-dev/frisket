// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry, type ActionCatalogEntry, type ActionCatalogPayload } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { hasServedActionCatalogPython, servedActionCatalog } from '../support/servedActionCatalog';

const SHEET = sheetMeta([
  columnDef({ id: '11', name: 'address', type: 'text' }),
  columnDef({ id: '12', name: 'house_number', type: 'integer' }),
  columnDef({ id: '13', name: 'opened_on', type: 'date' }),
  columnDef({ id: '14', name: 'point', type: 'geo_point' }),
  columnDef({ id: '15', name: '{{address}}', type: 'text' }),
], { id: '7', name: 'Places', rowCount: 3 });

type Kind = 'enrich.geocode' | 'enrich.census_demographics';
describe.skipIf(!hasServedActionCatalogPython() && !process.env.CI)('typed geospatial forms', () => {
  let catalog: ActionCatalogPayload;
  beforeAll(() => {
    installPopoverPolyfill();
    catalog = servedActionCatalog();
  }, 30_000);
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  function renderForm(kind: Kind, options: Partial<ComponentProps<typeof GeneratedActionForm>> = {},
    missingCredentials: string[] = [], runtimeHints: Partial<ActionCatalogEntry['ui_hints']> = {},
    engineDefault?: string) {
    const raw = catalog.actions.find((entry) => entry.kind === kind)!;
    if (!isGeneratedActionCatalogEntry(raw)) throw new Error(`${kind} must use its typed catalog contract`);
    const entry = { ...raw,
      ...(engineDefault ? { input_schema: { ...raw.input_schema, properties: {
        ...raw.input_schema.properties,
        engine: { ...raw.input_schema.properties?.engine, default: engineDefault },
      } } } : {}),
      ui_hints: { ...raw.ui_hints, ...runtimeHints, missing_credentials: missingCredentials } };
    const template = generatedActionTemplateFromCatalogEntry(entry)!;
    const resolveParams = vi.fn(async ({ params }: Parameters<ComponentProps<typeof GeneratedActionForm>['resolveParams']>[0]) => ({
      diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs.filter(({ key }) => (
        kind === 'enrich.geocode'
          ? params.include_lat_lon || !['latitude', 'longitude'].includes(key)
          : params.include_moe || !key.endsWith('_moe')
      )),
    }));
    const execute = vi.fn();
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template} sheet={SHEET}
      running={false} resolveParams={resolveParams} onExecute={execute} onClose={vi.fn()}
      estimateAction={vi.fn(async () => ({ cost: 0, rows: 3, billed_cost: 0 }))} {...options} />);
    return { entry, execute, resolveParams };
  }

  function chooseEngine(engineId: string): void {
    fireEvent.click(screen.getByTestId('engine-picker-button'));
    fireEvent.change(screen.getByTestId('engine-picker-search'), { target: { value: engineId } });
    fireEvent.click(screen.getByTestId(`engine-option-${engineId}`));
  }

  it('keeps address-column eligibility separate from template parts and sends canonical preview/run requests', async () => {
    const { execute } = renderForm('enrich.geocode');
    const column = screen.getByTestId('text-source-column-select') as HTMLSelectElement;
    expect(column).toHaveValue('address');
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent('Auto');
    expect(Array.from(column.options).map((option) => option.value)).not.toContain('house_number');
    const templateOption = Array.from(column.options).find((option) => option.text === 'Template')!;
    fireEvent.change(column, { target: { value: templateOption.value } });
    const composer = screen.getByTestId('text-source-template-input');
    fireEvent.change(composer, { target: { value: '{{house_number}} — {{opened_on}}' } });
    expect(screen.getByTestId('text-source-template-column-insert')).toHaveTextContent('house_number');
    chooseEngine('nominatim');
    fireEvent.click(screen.getByTestId('include-lat-lon-columns'));
    await waitFor(() => expect(screen.getByTestId('field-output-latitude')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('field-output-geo_point'), { target: { value: 'location' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('No provider/API charge');
    expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent(/\$|UNKNOWN/);
    expect(screen.getByTestId('cost-estimate')).not.toHaveClass('cost-paid');
    expect(screen.getByTestId('action-output-summary')).toHaveTextContent('latitude and longitude');
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(execute).toHaveBeenCalledTimes(2);
    for (const [request] of execute.mock.calls) {
      expect(request.action_id).toBe('enrich.geocode');
      expect(request.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
      expect(request.params).toEqual({ source: { text: '{{house_number}} — {{opened_on}}' },
        engine: 'nominatim', include_lat_lon: true });
      expect(request.output_names).toEqual({ geo_point: 'location', formatted_address: 'formatted_address',
        latitude: 'latitude', longitude: 'longitude' });
      expect(request).not.toHaveProperty('actionKind');
    }
    expect(execute.mock.calls.map((call) => call[1])).toEqual(['preview', 'run']);
  });

  it.each(['{{address}}', { text: 'address' }])('preserves the exact saved source branch (%j)', async (source) => {
    const params = { source, engine: 'nominatim', include_lat_lon: false };
    const { execute } = renderForm('enrich.geocode', { initialDraft: {
      action_id: 'enrich.geocode', scope: { kind: 'sheet_rows', sheet_id: 7 }, params,
      output_names: { geo_point: 'saved_point', formatted_address: 'saved_address' },
    } });
    if (typeof source === 'string') expect(screen.getByTestId('text-source-column-select')).toHaveValue(source);
    else expect(screen.getByTestId('text-source-template-input')).toHaveValue(source.text);
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(execute.mock.calls[0][0].params).toEqual(params);
    expect(execute.mock.calls[0][0].output_names).toEqual({
      geo_point: 'saved_point', formatted_address: 'saved_address',
    });
  });

  it('preserves Census geography, MOE, source and all saved output names', async () => {
    const raw = catalog.actions.find((entry) => entry.kind === 'enrich.census_demographics')!;
    const outputNames = Object.fromEntries(raw.ui_hints.logical_outputs!.map(({ key }) => [key, `saved_${key}`]));
    const params = { source: 'point', geography: 'block_group', include_moe: true };
    const { execute } = renderForm('enrich.census_demographics', { initialDraft: {
      action_id: 'enrich.census_demographics', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params, output_names: outputNames,
    } });
    const source = screen.getByRole('combobox', { name: 'Geo point column' }) as HTMLSelectElement;
    expect(Array.from(source.options).map((option) => option.value)).toEqual(['point']);
    expect(screen.getByTestId('field-geography')).toHaveValue('block_group');
    expect(screen.getByTestId('field-geography')).toHaveTextContent('Census block group');
    expect(screen.getByTestId('field-include_moe')).toBeChecked();
    expect(screen.queryByTestId('field-provider')).not.toBeInTheDocument();
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('US Census ACS enrichment');
    expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent(/\$|per row/);
    expect(screen.getByTestId('cost-estimate')).not.toHaveClass('cost-paid');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(Object.keys(outputNames)).toHaveLength(22);
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(execute.mock.calls[0][0].params).toEqual(params);
    expect(execute.mock.calls[0][0].output_names).toEqual(outputNames);
  });

  it('keeps the Census missing-key refusal and existing Settings remedy', async () => {
    const { execute } = renderForm('enrich.census_demographics', {}, ['CENSUS_API_KEY']);
    expect(screen.getByTestId('action-credential-gate')).toHaveTextContent('CENSUS_API_KEY');
    await waitFor(() => expect(screen.queryByText('Validating fields…')).not.toBeInTheDocument());
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('action-credential-gate-settings-link'));
    expect(window.location.search).toBe('?secret=CENSUS_API_KEY');
    expect(execute).not.toHaveBeenCalled();
  });

  it('does not borrow the default OpenCage price for an explicit Nominatim selection', async () => {
    renderForm('enrich.geocode', { initialDraft: { action_id: 'enrich.geocode',
      scope: { kind: 'sheet_rows', sheet_id: 7 }, params: { source: 'address', engine: 'nominatim' },
      output_names: { geo_point: 'location', formatted_address: 'normalized' } } }, [], {
      cost_source: 'pricing_data', cost_source_options: { nominatim: 'free_public_api' },
      pricing: { key: 'geocode.opencage.row', label: 'OpenCage geocoder request', provider: 'OpenCage',
        unit: 'row', unit_price_usd: 0.01, billable: true, external_api: true, cost_source: 'pricing_data' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    const line = screen.getByTestId('cost-estimate');
    expect(line).toHaveTextContent('No provider/API charge');
    expect(line).not.toHaveTextContent(/OpenCage|\$|UNKNOWN/);
  });

  it.each(['unavailable', 'missing', 'missing-default'] as const)(
    'preserves a %s saved engine and refuses execution without silently choosing another', async (availability) => {
    const { execute } = renderForm('enrich.geocode', { initialDraft: {
      action_id: 'enrich.geocode', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'address', engine: 'opencage', include_lat_lon: false },
      output_names: { geo_point: 'location', formatted_address: 'normalized' },
    } }, [], { engines: [
      ...(availability === 'unavailable' ? [{ id: 'opencage', label: 'OpenCage', tier: 'hosted' as const,
        available: false, error: 'Configure OPENCAGE_API_KEY.' }] : []),
      { id: 'nominatim', label: 'Nominatim', tier: 'hosted', available: true },
    ] }, availability === 'missing-default' ? 'opencage' : undefined);
    // Wait for validation: an initially disabled button does not prove the
    // catalog availability gate survives successful parameter resolution.
    await screen.findByTestId('field-output-geo_point');
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent(/opencage/i);
    expect(screen.getByRole('alert')).toHaveTextContent(
      availability === 'unavailable' ? 'OPENCAGE_API_KEY' : 'opencage is unavailable.',
    );
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    expect(execute).not.toHaveBeenCalled();
    chooseEngine('nominatim');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(execute.mock.calls[0][0].params).toEqual({ source: 'address', engine: 'nominatim', include_lat_lon: false });
  });

  it('keeps the admitted automatic default available without inventing a provider roster entry', async () => {
    const { execute } = renderForm('enrich.geocode', {}, [], {
      engines: [{ id: 'nominatim', label: 'Nominatim', tier: 'hosted', available: true }],
    });
    await screen.findByTestId('field-output-geo_point');
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent('Auto');
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(execute.mock.calls[0][0].params.engine).toBe('auto');
  });

  it('displays and gates an omitted saved engine default without adding it to Params', async () => {
    const { execute } = renderForm('enrich.geocode', { initialDraft: {
      action_id: 'enrich.geocode', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'address' }, output_names: { geo_point: 'Saved point', formatted_address: 'Saved address' },
    } }, [], { engines: [{ id: 'nominatim', label: 'Nominatim', tier: 'hosted', available: false }] });
    await screen.findByTestId('field-output-geo_point');
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent('Auto');
    expect(screen.getByRole('alert')).toHaveTextContent('No execution engines are available.');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(execute).not.toHaveBeenCalled();
  });

  it.each([
    { label: 'empty', engines: [] },
    { label: 'unavailable', engines: [
      { id: 'nominatim', label: 'Nominatim', tier: 'hosted' as const, available: false },
    ] },
  ])('refuses auto when its execution engine roster is $label', async ({ engines }) => {
    const { execute } = renderForm('enrich.geocode', {}, [], { engines });
    await screen.findByTestId('field-output-geo_point');
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent('Auto');
    expect(screen.getByRole('alert')).toHaveTextContent('No execution engines are available.');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    expect(execute).not.toHaveBeenCalled();
  });

  it('retains the configure-first geocode handoff when no semantic point exists', () => {
    const navigate = vi.fn();
    renderForm('enrich.census_demographics', { sheet: { ...SHEET,
      columns: SHEET.columns.filter((column) => column.type !== 'geo_point') },
    onNavigateToAction: navigate }, ['CENSUS_API_KEY']);
    expect(screen.getByTestId('census-no-geo-point')).toHaveTextContent('No compatible geo_point columns.');
    fireEvent.click(screen.getByRole('button', { name: 'Run Geocode first' }));
    expect(navigate).toHaveBeenCalledWith('enrich.geocode');
  });
});
