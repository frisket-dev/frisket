import { PanelSelect } from '../PanelSelect';
import { UnitInput } from './ActionParams';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export function PageCaptureParamsBody({ params, setParams, errors, Field }:
  GeneratedActionParamsBodyProps<'web.capture_page'>) {
  const mode = params.output_mode ?? 'page';
  const render = params.render_mode ?? 'static';
  return <>
    <label className="field-group"><span className="field-labels">Collect</span>
      <PanelSelect className="form-input" data-testid="field-output_mode" value={mode}
        onChange={(event) => setParams({ ...params,
          output_mode: event.target.value as 'page' | 'links',
          ...(event.target.value === 'links' && params.include_warc ? { include_warc: false } : {}),
        })} options={[
          { value: 'page', label: 'Full pages', description: 'Save each page as an HTML artifact' },
          { value: 'links', label: 'Links only', description: 'Create one child-sheet row per unique page link' },
        ]} />
    </label>
    <Field name="source" label="Page links come from" />
    <p className="form-hint">{mode === 'links'
      ? 'Each unique HTTP(S) link becomes a row with its anchor text and source-row lineage.'
      : 'The column holding the URLs you want to snapshot.'}</p>
    <label className="field-group"><span className="field-labels">How to load each page</span>
      <PanelSelect className="form-input" data-testid="field-render_mode" value={render}
        onChange={(event) => setParams({ ...params,
          render_mode: event.target.value as 'static' | 'playwright',
          ...(event.target.value === 'static' && params.include_warc ? { include_warc: false } : {}),
        })} options={[
          { value: 'static', label: 'Static', description: 'Guarded HTTP fetch — fast, no browser' },
          { value: 'playwright', label: 'Dynamic', description: 'Full page render with a headless browser for JS-heavy pages and network metadata' },
        ]} />
    </label>
    <p className="form-hint">Static is a fast guarded HTTP fetch. Dynamic runs a real browser for JS-heavy pages.</p>
    <details className="action-advanced" data-testid="page-capture-advanced">
      <summary>Capture limits and archive options</summary>
      {mode === 'page' && render === 'playwright' && <>
        <Field name="include_warc" label="Include WARC" />
        <p className="form-hint">Save the browser network archive alongside the page.</p>
      </>}
      <label className="field-group"><span className="field-labels">Max size</span>
        <UnitInput id="capture-max-bytes" testid="field-max_bytes" value={String(params.max_bytes ?? 5_000_000)}
          onChange={(value) => setParams({ ...params, max_bytes: Number(value) })}
          unit={{ defaultUnit: 'MB', units: [
            { value: 'B', multiplier: 1 }, { value: 'KB', multiplier: 1000 },
            { value: 'MB', multiplier: 1_000_000 }, { value: 'GB', multiplier: 1_000_000_000 },
          ] }} />
        {errors.max_bytes?.message && <span className="form-error">{errors.max_bytes.message}</span>}
      </label>
      <label className="field-group"><span className="field-labels">Timeout</span>
        <UnitInput id="capture-timeout" testid="field-timeout_ms" value={String(params.timeout_ms ?? 30_000)}
          onChange={(value) => setParams({ ...params, timeout_ms: Number(value) })}
          unit={{ defaultUnit: 's', units: [{ value: 'ms', multiplier: 1 }, { value: 's', multiplier: 1000 }] }} />
        {errors.timeout_ms?.message && <span className="form-error">{errors.timeout_ms.message}</span>}
      </label>
    </details>
  </>;
}
