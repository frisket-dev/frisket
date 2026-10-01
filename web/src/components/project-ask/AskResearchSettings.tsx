import { PanelSelect } from '../PanelSelect';
import './AskResearch.css';

export type AskResearchWriteMode = 'ask_each' | 'ask_overwrite' | 'full_access';

export interface AskResearchOptions {
  write_mode: AskResearchWriteMode;
  budget_usd: string | null;
  max_turns: number | null;
  /** An empty list lets research use every enabled skill. */
  skills: string[];
}

export interface AskResearchSkill {
  id: string;
  name: string;
  enabled: boolean;
}

export interface AskResearchSettingsProps {
  value: AskResearchOptions | undefined;
  onChange(value: AskResearchOptions | undefined): void;
  availableSkills: readonly AskResearchSkill[];
  effectiveWebProvider?: string | null;
  disabled?: boolean;
}

const DEFAULT_RESEARCH: AskResearchOptions = {
  write_mode: 'ask_overwrite',
  budget_usd: null,
  max_turns: null,
  skills: [],
};

const MONEY_PATTERN = /^\d+(?:\.\d{0,6})?$/;

export function AskResearchSettings({
  value,
  onChange,
  availableSkills,
  effectiveWebProvider,
  disabled = false,
}: AskResearchSettingsProps) {
  const enabledSkills = availableSkills.filter((skill) => skill.enabled);
  const selectedSkills = value?.skills.length ? new Set(value.skills) : new Set(enabledSkills.map((skill) => skill.id));
  const update = (change: Partial<AskResearchOptions>) => {
    if (value) onChange({ ...value, ...change });
  };

  const toggleSkill = (id: string, checked: boolean) => {
    if (!value) return;
    const selected = new Set(selectedSkills);
    if (checked) selected.add(id);
    else selected.delete(id);
    const next = enabledSkills.filter((skill) => selected.has(skill.id)).map((skill) => skill.id);
    update({ skills: next.length === enabledSkills.length ? [] : next });
  };

  return <section className="ask-research-settings" aria-label="Research settings">
    <label className="ask-option-check ask-research-toggle">
      <input
        type="checkbox"
        aria-label="Automatic research"
        checked={value !== undefined}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked ? { ...DEFAULT_RESEARCH } : undefined)}
      />
      <span><strong>Automatic research</strong><small>Continue investigating and running approved actions in the background.</small></span>
    </label>

    {value && <div className="ask-research-fields">
      <label className="ask-research-field">
        <span>Write access</span>
        <PanelSelect
          value={value.write_mode}
          disabled={disabled}
          topLayer
          aria-label="Write access"
          options={[
            { value: 'ask_each', label: 'Ask before each action' },
            { value: 'ask_overwrite', label: 'Ask before overwriting' },
            { value: 'full_access', label: 'Full access' },
          ]}
          onValueChange={(writeMode) => update({ write_mode: writeMode as AskResearchWriteMode })}
        />
      </label>

      <label className="ask-research-field">
        <span>Total budget (USD)</span>
        <input
          className="form-input"
          type="text"
          inputMode="decimal"
          aria-label="Total budget (USD)"
          placeholder="Current approval limit"
          value={value.budget_usd ?? ''}
          disabled={disabled}
          onChange={(event) => {
            const next = event.target.value.trim();
            if (!next || MONEY_PATTERN.test(next)) update({ budget_usd: next || null });
          }}
        />
        <small>Uses your current action approval limit when blank.</small>
      </label>

      <label className="ask-research-field">
        <span>Max turns</span>
        <input
          className="form-input"
          type="number"
          min={1}
          step={1}
          aria-label="Max turns"
          placeholder="No limit"
          value={value.max_turns ?? ''}
          disabled={disabled}
          onChange={(event) => {
            const raw = event.target.value;
            if (!raw) update({ max_turns: null });
            else if (/^\d+$/.test(raw) && Number(raw) > 0) update({ max_turns: Number(raw) });
          }}
        />
        <small>Leave blank for no limit.</small>
      </label>

      <fieldset className="ask-research-skills" disabled={disabled}>
        <legend>Skills</legend>
        {enabledSkills.length === 0
          ? <small>No enabled skills are available.</small>
          : enabledSkills.map((skill) => <label key={skill.id}>
            <input
              type="checkbox"
              checked={selectedSkills.has(skill.id)}
              onChange={(event) => toggleSkill(skill.id, event.target.checked)}
            />
            <span>{skill.name}</span>
          </label>)}
        {enabledSkills.length > 0 && <small>All enabled skills are available when every skill is selected.</small>}
      </fieldset>

      {effectiveWebProvider && <p className="ask-research-provider">Web searches use {effectiveWebProvider}.</p>}
    </div>}
  </section>;
}
