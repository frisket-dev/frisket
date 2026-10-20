import { Button } from '../Button';
import { TextInput } from '../FormControls';
import { PanelSelect } from '../PanelSelect';
import type { SourceInterval } from '../../api/open';
import { INTERVALS, SOURCE_KINDS, selectedKind, type KnownSourceKind, type SourceFormState } from './sourceConfig';
import { SourceConfigFields } from './SourceConfigFields';
import type { SourceKindFormFrame } from './sourceFrames';
import type { BusySource } from './sourceState';
import type { SourcePanelActions } from './useSourcesPanelState';
import '../SourcesPanel.css';

interface SourceEditorFormProps {
  actions: SourcePanelActions;
  busy: BusySource;
  form: SourceFormState;
  formFrame?: SourceKindFormFrame;
}

export function SourceEditorForm({ actions, busy, form, formFrame }: SourceEditorFormProps) {
  const kind = selectedKind(form);
  const Frame = formFrame;

  const formElement = (
    <form className="content-stack" data-testid="source-form" onSubmit={actions.submit}>
      <div className="sources-form-row">
        <PanelSelect
          className="form-input"
          aria-label="Source type"
          data-testid="source-kind"
          value={form.kind}
          onChange={(e) => actions.updateForm({ kind: e.target.value as KnownSourceKind })}
        >
          {SOURCE_KINDS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </PanelSelect>
      </div>
      <TextInput
        placeholder="Source name"
        aria-label="Source name"
        data-testid="source-name"
        value={form.name}
        onChange={(e) => actions.updateForm({ name: e.target.value })}
      />
      <label className="form-label" htmlFor="source-url-input">
        {kind.urlLabel}
      </label>
      <TextInput
        id="source-url-input"

        placeholder={kind.placeholder}
        aria-label={kind.urlLabel}
        data-testid="source-url"
        value={form.url}
        onChange={(e) => actions.updateForm({ url: e.target.value })}
      />

      <SourceConfigFields form={form} onChange={actions.updateForm} />
      {form.kind === 'courtlistener_docket' && <div className="panel-item-meta">RECAP PDFs stay link-only. Runtime tokens are not stored in source config.</div>}

      <PanelSelect
        className="form-input"
        aria-label="Polling cadence"
        data-testid="source-interval"
        value={form.cadence}
        onChange={(e) => actions.updateForm({ cadence: e.target.value as SourceInterval })}
      >
        {INTERVALS.map((i) => (
          <option key={i.value} value={i.value}>{i.label}</option>
        ))}
      </PanelSelect>
      <div className="control-row" data-align="end">
        <Button
          type="button"

          onClick={() => actions.setAdding(false)}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          className="btn-primary"
          data-testid="source-create"
          disabled={busy !== null || !form.name.trim() || !form.url.trim()}
        >
          {busy !== null
            ? (form.editingId == null ? 'Adding...' : 'Saving...')
            : (form.editingId == null ? 'Add' : 'Save')}
        </Button>
      </div>
    </form>
  );

  if (!Frame) return formElement;
  return <Frame sourceKind={form.kind}>{formElement}</Frame>;
}
