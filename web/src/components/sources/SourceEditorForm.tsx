import { Button } from '../Button';
import { FormField, TextInput, TextArea } from '../FormControls';
import { PanelSelect } from '../PanelSelect';
import type { SourceInterval } from '../../api/open';
import { INTERVALS, SOURCE_KINDS, selectedKind, type KnownSourceKind, type SourceFormState } from './sourceConfig';
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

      {(form.kind === 'youtube_playlist' || form.kind === 'youtube_channel') && (
        <div className="sources-form-grid">
          <FormField label="Max pages" >

            <TextInput
              aria-label="YouTube max pages"
              data-testid="source-youtube-max-pages"
              value={form.youtubeMaxPages}
              onChange={(e) => actions.updateForm({ youtubeMaxPages: e.target.value })}
            />
          </FormField>
          <FormField label="Max items" >

            <TextInput
              aria-label="YouTube max items"
              data-testid="source-youtube-max-items"
              value={form.youtubeMaxItems}
              onChange={(e) => actions.updateForm({ youtubeMaxItems: e.target.value })}
            />
          </FormField>
        </div>
      )}

      {form.kind === 'api_list_dicts' && (
        <>
          <div className="sources-form-grid">
            <FormField label="List path" >

              <TextInput
                aria-label="API list path"
                data-testid="source-api-list-path"
                value={form.apiListPath}
                onChange={(e) => actions.updateForm({ apiListPath: e.target.value })}
              />
            </FormField>
            <FormField label="Item ID path" >

              <TextInput
                aria-label="API item ID path"
                data-testid="source-api-item-id-path"
                value={form.apiItemIdPath}
                onChange={(e) => actions.updateForm({ apiItemIdPath: e.target.value })}
              />
            </FormField>
          </div>
          <div className="sources-form-grid">
            <FormField label="Updated path" >

              <TextInput
                aria-label="API updated-at path"
                data-testid="source-api-updated-at-path"
                value={form.apiUpdatedAtPath}
                onChange={(e) => actions.updateForm({ apiUpdatedAtPath: e.target.value })}
              />
            </FormField>
            <FormField label="Max items" >

              <TextInput
                aria-label="API max items"
                data-testid="source-api-max-items"
                value={form.apiMaxItems}
                onChange={(e) => actions.updateForm({ apiMaxItems: e.target.value })}
              />
            </FormField>
          </div>
        </>
      )}

      {form.kind === 'courtlistener_docket' && (
        <>
          <TextArea
            className="source-textarea"
            aria-label="CourtListener keywords"
            data-testid="source-court-keywords"
            value={form.courtKeywords}
            onChange={(e) => actions.updateForm({ courtKeywords: e.target.value })}
            placeholder="injunction, settlement"
          />
          <div className="control-row panel-item-description">
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="source-court-include-parties"
                checked={form.courtIncludeParties}
                onChange={(e) => actions.updateForm({ courtIncludeParties: e.target.checked })}
              />
              Parties
            </label>
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="source-court-include-documents"
                checked={form.courtIncludeDocuments}
                onChange={(e) => actions.updateForm({ courtIncludeDocuments: e.target.checked })}
              />
              Documents
            </label>
          </div>
          <label className="form-label form-field">
            Max entries
            <TextInput
              aria-label="CourtListener max entries"
              data-testid="source-court-max-entries"
              value={form.courtMaxEntries}
              onChange={(e) => actions.updateForm({ courtMaxEntries: e.target.value })}
            />
          </label>
          <div className="panel-item-meta">RECAP PDFs stay link-only. Runtime tokens are not stored in source config.</div>
        </>
      )}

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
