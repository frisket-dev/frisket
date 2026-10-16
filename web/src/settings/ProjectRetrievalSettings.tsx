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
  return (
    <section className="settings-subsection" data-testid="project-retrieval-settings">
      <div className="settings-section-subhead">
        <h2>Search preparation</h2>
        <p>
          Choose which source content Frisket prepares for semantic search. Changes rebuild
          affected search inputs in the background.
        </p>
      </div>
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
              {help && <small className="settings-field-help">{help}</small>}
            </span>
          </label>
        ))}
      </div>
      <label className="settings-field">
        <span>Video indexing</span>
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
        <small className="settings-field-help">
          Periodic frames process less video. Fixed clips use overlapping time windows.
        </small>
      </label>
    </section>
  );
}
