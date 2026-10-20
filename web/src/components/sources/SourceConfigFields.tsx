import { FormField, TextInput, TextArea } from '../FormControls';
import type { SourceDefinitionDraft } from './sourceConfig';
import '../SourcesPanel.css';

/** Source-kind config authoring shared by import and editing. */
export function SourceConfigFields({ form, onChange }: {
  form: SourceDefinitionDraft;
  onChange(patch: Partial<SourceDefinitionDraft>): void;
}) {
  return (
    <>
      {(form.kind === 'youtube_playlist' || form.kind === 'youtube_channel') && (
        <div className="sources-form-grid">
          <FormField label="Max pages" >

            <TextInput
              aria-label="YouTube max pages"
              data-testid="source-youtube-max-pages"
              value={form.youtubeMaxPages}
              onChange={(e) => onChange({ youtubeMaxPages: e.target.value })}
            />
          </FormField>
          <FormField label="Max items" >

            <TextInput
              aria-label="YouTube max items"
              data-testid="source-youtube-max-items"
              value={form.youtubeMaxItems}
              onChange={(e) => onChange({ youtubeMaxItems: e.target.value })}
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
                onChange={(e) => onChange({ apiListPath: e.target.value })}
              />
            </FormField>
            <FormField label="Item ID path" >

              <TextInput
                aria-label="API item ID path"
                data-testid="source-api-item-id-path"
                value={form.apiItemIdPath}
                onChange={(e) => onChange({ apiItemIdPath: e.target.value })}
              />
            </FormField>
          </div>
          <div className="sources-form-grid">
            <FormField label="Updated path" >

              <TextInput
                aria-label="API updated-at path"
                data-testid="source-api-updated-at-path"
                value={form.apiUpdatedAtPath}
                onChange={(e) => onChange({ apiUpdatedAtPath: e.target.value })}
              />
            </FormField>
            <FormField label="Max items" >

              <TextInput
                aria-label="API max items"
                data-testid="source-api-max-items"
                value={form.apiMaxItems}
                onChange={(e) => onChange({ apiMaxItems: e.target.value })}
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
            onChange={(e) => onChange({ courtKeywords: e.target.value })}
            placeholder="injunction, settlement"
          />
          <div className="control-row panel-item-description">
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="source-court-include-parties"
                checked={form.courtIncludeParties}
                onChange={(e) => onChange({ courtIncludeParties: e.target.checked })}
              />
              Parties
            </label>
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="source-court-include-documents"
                checked={form.courtIncludeDocuments}
                onChange={(e) => onChange({ courtIncludeDocuments: e.target.checked })}
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
              onChange={(e) => onChange({ courtMaxEntries: e.target.value })}
            />
          </label>
        </>
      )}

    </>
  );
}
