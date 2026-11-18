import { FormField, TextInput, TextArea } from '../FormControls';
import type { FeedDefinitionDraft } from './feedConfig';
import '../FeedsPanel.css';

/** Source-kind config authoring shared by import and editing. */
export function FeedConfigFields({ form, onChange }: {
  form: FeedDefinitionDraft;
  onChange(patch: Partial<FeedDefinitionDraft>): void;
}) {
  return (
    <>
      {(form.kind === 'youtube_playlist' || form.kind === 'youtube_channel' || form.kind === 'tiktok_profile') && (
        <div className="feeds-form-grid">
          {form.kind !== 'tiktok_profile' && <FormField label="Max pages" >

            <TextInput
              aria-label="YouTube max pages"
              data-testid="feed-youtube-max-pages"
              value={form.youtubeMaxPages}
              onChange={(e) => onChange({ youtubeMaxPages: e.target.value })}
            />
          </FormField>}
          <FormField label="Max items" >

            <TextInput
              aria-label={form.kind === 'tiktok_profile' ? 'TikTok max items' : 'YouTube max items'}
              data-testid={form.kind === 'tiktok_profile' ? 'source-tiktok-max-items' : 'source-youtube-max-items'}
              value={form.mediaMaxItems}
              onChange={(e) => onChange({ mediaMaxItems: e.target.value })}
            />
          </FormField>
        </div>
      )}

      {form.kind === 'api_list_dicts' && (
        <>
          <div className="feeds-form-grid">
            <FormField label="List path" >

              <TextInput
                aria-label="API list path"
                data-testid="feed-api-list-path"
                value={form.apiListPath}
                onChange={(e) => onChange({ apiListPath: e.target.value })}
              />
            </FormField>
            <FormField label="Item ID path" >

              <TextInput
                aria-label="API item ID path"
                data-testid="feed-api-item-id-path"
                value={form.apiItemIdPath}
                onChange={(e) => onChange({ apiItemIdPath: e.target.value })}
              />
            </FormField>
          </div>
          <div className="feeds-form-grid">
            <FormField label="Updated path" >

              <TextInput
                aria-label="API updated-at path"
                data-testid="feed-api-updated-at-path"
                value={form.apiUpdatedAtPath}
                onChange={(e) => onChange({ apiUpdatedAtPath: e.target.value })}
              />
            </FormField>
            <FormField label="Max items" >

              <TextInput
                aria-label="API max items"
                data-testid="feed-api-max-items"
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
            className="feed-textarea"
            aria-label="CourtListener keywords"
            data-testid="feed-court-keywords"
            value={form.courtKeywords}
            onChange={(e) => onChange({ courtKeywords: e.target.value })}
            placeholder="injunction, settlement"
          />
          <div className="control-row panel-item-description">
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="feed-court-include-parties"
                checked={form.courtIncludeParties}
                onChange={(e) => onChange({ courtIncludeParties: e.target.checked })}
              />
              Parties
            </label>
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="feed-court-include-documents"
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
              data-testid="feed-court-max-entries"
              value={form.courtMaxEntries}
              onChange={(e) => onChange({ courtMaxEntries: e.target.value })}
            />
          </label>
        </>
      )}

    </>
  );
}
