import { PanelSelect } from '../PanelSelect';
import type { AskResearchConfiguration, AskResearchOptions } from '../../api/projectQA';
import './AskResearch.css';

export type AskResearchWriteMode = 'ask_each' | 'ask_overwrite' | 'full_access';
export type { AskResearchOptions } from '../../api/projectQA';
export type AskResearchSkill = AskResearchConfiguration['skills'][number];

export interface AskResearchSettingsProps {
  value: AskResearchOptions | undefined;
  onChange(value: AskResearchOptions | undefined): void;
  availableSkills: readonly AskResearchSkill[];
  effectiveWebProvider?: string | null;
  defaultBudgetUsd?: string | null;
  availabilityMessage?: string | null;
  loading?: boolean;
  onRetry?(): void;
  disabled?: boolean;
}

const DEFAULT_RESEARCH: AskResearchOptions = {
  write_mode: 'ask_overwrite',
  budget_usd: null,
  max_turns: null,
  skills: null,
};

const MONEY_PATTERN = /^\d+(?:\.\d{0,6})?$/;

export function AskResearchSettings({
  value,
  onChange,
  availableSkills,
  effectiveWebProvider,
  defaultBudgetUsd,
  availabilityMessage,
  loading = false,
  onRetry,
  disabled = false,
}: AskResearchSettingsProps) {
  const selectedSkills = new Set(value?.skills ?? availableSkills.map((skill) => skill.name));
  const update = (change: Partial<AskResearchOptions>) => {
    if (value) onChange({ ...value, ...change });
  };

  const toggleSkill = (name: string, checked: boolean) => {
    if (!value) return;
    const selected = new Set(selectedSkills);
    if (checked) selected.add(name);
    else selected.delete(name);
    const next = availableSkills.filter((skill) => selected.has(skill.name)).map((skill) => skill.name);
    update({ skills: next.length === availableSkills.length ? null : next });
  };

  return <section className="ask-research-settings" aria-label="Research settings">
    <label className="ask-option-check ask-research-toggle">
      <input
        type="checkbox"
        aria-label="Run actions"
        checked={value !== undefined}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked ? { ...DEFAULT_RESEARCH } : undefined)}
      />
      <span><strong>Run actions</strong><small>Continue investigating and running approved actions in the background.</small></span>
    </label>
    {availabilityMessage && <p className="ask-research-availability" role={onRetry ? 'alert' : undefined}>
      <span>{availabilityMessage}</span>
      {onRetry && <button type="button" className="btn" disabled={loading} onClick={onRetry}>{loading ? 'Retrying…' : 'Retry'}</button>}
    </p>}

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
        <small>Uses your current action approval limit{defaultBudgetUsd ? ` (${`$${defaultBudgetUsd}`})` : ''} when blank.</small>
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
        {availableSkills.length === 0
          ? <small>No enabled skills are available.</small>
          : availableSkills.map((skill) => <label key={skill.name}>
            <input
              type="checkbox"
              aria-label={skill.name}
              checked={selectedSkills.has(skill.name)}
              onChange={(event) => toggleSkill(skill.name, event.target.checked)}
            />
            <span>{skill.name}{skill.description && <small>{skill.description}</small>}</span>
          </label>)}
        {availableSkills.length > 0 && <small>All enabled skills are available when every skill is selected.</small>}
      </fieldset>

      {effectiveWebProvider && <p className="ask-research-provider">Web searches use {effectiveWebProvider}.</p>}
    </div>}
  </section>;
}
