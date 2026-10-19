import '../components/SearchPreparationReceipt.css';
import './SettingsPrimitives.css';
import { FormField } from '../components/FormControls';
import type {
  ProjectRetrievalSettings as ProjectRetrievalSettingsValue,
  ProjectSettingsPatch,
} from '../api/types';
import { PanelSelect, type PanelSelectOption } from '../components/PanelSelect';

const VIDEO_STRATEGY_OPTIONS: PanelSelectOption[] = [
  {
    value: 'off',
    label: 'Off',
    description: 'Do not prepare video content for semantic search.',
  },
  {
    value: 'frames',
    label: 'Periodic frames',
    description: 'Index individual frames for a lower-cost visual search option.',
  },
  {
    value: 'fixed_clips',
    label: 'Fixed overlapping clips',
    description: 'Index short overlapping clips so nearby moments share context.',
  },
  {
    value: 'shot_clips',
    label: 'Shot-aware clips (default)',
    description: 'Split clips at scene changes and limit long shots.',
  },
];

interface ProjectRetrievalSettingsProps {
  settings: ProjectRetrievalSettingsValue;
  canMutate: boolean;
  onPatch(input: ProjectSettingsPatch): void | Promise<void>;
}

const SOURCE_OPTIONS: ReadonlyArray<{
  field: keyof Pick<
    ProjectRetrievalSettingsValue,
    'text_enabled' | 'transcript_enabled' | 'image_enabled' | 'pdf_pages_enabled' | 'audio_enabled'
  >;
  label: string;
  help?: string;
}> = [
  { field: 'text_enabled', label: 'Text cells' },
  { field: 'transcript_enabled', label: 'Existing transcripts' },
  { field: 'image_enabled', label: 'Images' },
  { field: 'pdf_pages_enabled', label: 'PDF pages' },
  {
    field: 'audio_enabled',
    label: 'Raw audio',
    help: 'Optional fallback when an existing transcript is unavailable or insufficient.',
  },
];

export function ProjectRetrievalSettings({
  settings,
  canMutate,
  onPatch,
}: ProjectRetrievalSettingsProps) {
  const automaticPreparation = settings.prepare_new_files_automatically;
  return (
    <section className="content-stack" data-testid="project-retrieval-settings">
      <div className="settings-section-subhead">
        <h2>Search preparation</h2>
        <p>
          Choose which source content Frisket prepares for semantic search. Changes rebuild
          affected search inputs in the background.
        </p>
      </div>
      <label className="settings-check search-preparation-setting">
        <input
          type="checkbox"
          data-testid="search-preparation-setting"
          checked={automaticPreparation}
          disabled={!canMutate}
          onChange={(event) => void onPatch({
            retrieval: {
              prepare_new_files_automatically: event.currentTarget.checked,
            },
          })}
        />
        <span>
          Prepare new files automatically
          <small className="form-hint">
            Read PDFs and images and transcribe recordings after import. Missing local models
            are downloaded only after you approve them.
          </small>
        </span>
      </label>
      <div className="settings-grid-two" role="group" aria-label="Source types">
        {SOURCE_OPTIONS.map(({ field, label, help }) => (
          <label className="settings-check" key={field}>
            <input
              type="checkbox"
              data-testid={`project-retrieval-${field}`}
              checked={settings[field]}
              disabled={!canMutate}
              onChange={(event) => void onPatch({
                retrieval: { [field]: event.currentTarget.checked },
              })}
            />
            <span>
              {label}
              {help && <small className="form-hint">{help}</small>}
            </span>
          </label>
        ))}
      </div>
      <FormField label="Video indexing" >

        <PanelSelect
          testId="project-retrieval-video-strategy"
          ariaLabel="Video indexing"
          value={settings.video.strategy}
          disabled={!canMutate}
          options={VIDEO_STRATEGY_OPTIONS}
          onValueChange={(strategy) => void onPatch({
            retrieval: {
              video: {
                strategy: strategy as ProjectRetrievalSettingsValue['video']['strategy'],
              },
            },
          })}
        />
        <small className="form-hint">
          Periodic frames process less video. Fixed clips use overlapping time windows.
        </small>
      </FormField>
    </section>
  );
}
