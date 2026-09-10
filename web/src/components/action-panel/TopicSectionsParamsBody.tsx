import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { PanelSelect } from '../PanelSelect';

export function TopicSectionsParamsBody({ params, setParams, Field }:
  GeneratedActionParamsBodyProps<'map.find_topic_sections'>) {
  return <>
    <Field name="source" label="Timestamped transcript" />
    <Field name="engine" />
    <label className="field-group">
      <span className="form-label">Detail</span>
      <PanelSelect data-testid="field-detail"
        value={typeof params.settings?.detail === 'string' ? params.settings.detail : 'balanced'}
        onChange={(event) => setParams({ ...params,
          settings: { ...params.settings, detail: event.target.value },
        })}>
        <option value="fewer">Fewer sections</option>
        <option value="balanced">Balanced</option>
        <option value="more">More sections</option>
      </PanelSelect>
    </label>
    <p className="form-hint">Find topic changes locally and save editable timeline ranges.</p>
    <details className="action-advanced">
      <summary>Segmentation settings</summary>
      <Field name="settings" />
    </details>
  </>;
}
