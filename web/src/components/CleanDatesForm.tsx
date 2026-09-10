import { useState } from 'react';
import { PanelSelect } from './PanelSelect';

/**
 * Controlled clean_dates format chooser. A blank format is the operation's
 * deterministic auto-detect mode; only Custom exposes an arbitrary strftime
 * field. Presets are deliberately exact wire values rather than examples.
 */

const PRESET_FORMATS = [
  { format: '%m/%d/%Y', sample: '03/14/2024' },
  { format: '%d/%m/%Y', sample: '14/03/2024' },
  { format: '%Y-%m-%d', sample: '2024-03-14' },
  { format: '%Y/%m/%d', sample: '2024/03/14' },
  { format: '%b %d, %Y', sample: 'Mar 14, 2024' },
  { format: '%B %d, %Y', sample: 'March 14, 2024' },
  { format: '%d %B %Y', sample: '14 March 2024' },
] as const;

type PresetFormat = (typeof PRESET_FORMATS)[number]['format'];
type FormatChoice = 'auto' | 'custom' | PresetFormat;

const FORMAT_OPTIONS = [
  { value: 'auto', label: 'Auto-detect' },
  ...PRESET_FORMATS.map((preset) => ({
    value: preset.format,
    label: `${preset.format} — ${preset.sample}`,
  })),
  { value: 'custom', label: 'Custom strftime' },
];

export interface CleanDatesFormProps {
  /** Explicit strftime override; blank delegates to the parser's auto mode. */
  format: string;
  onFormatChange: (format: string) => void;
}

function choiceForFormat(format: string, customOpen: boolean): FormatChoice {
  if (customOpen) return 'custom';
  if (!format) return 'auto';
  return PRESET_FORMATS.some((preset) => preset.format === format)
    ? (format as PresetFormat)
    : 'custom';
}

export function CleanDatesForm({ format, onFormatChange }: CleanDatesFormProps) {
  // A blank Custom field is still Custom, although it has the same wire value
  // as Auto. The parent remains the source of the format value; this tiny bit
  // of state only keeps the input visible while the user starts typing.
  const [customOpen, setCustomOpen] = useState(false);
  const choice = choiceForFormat(format, customOpen);

  return (
    <div className="clean-dates-form" data-testid="clean-dates-form">
      <div className="clean-dates-formats" data-testid="clean-dates-formats">
        <label className="form-label" htmlFor="clean-dates-format-select">
          What do your dates look like?
        </label>
        <PanelSelect
          id="clean-dates-format-select"
          testId="clean-dates-format-select"
          ariaLabel="What do your dates look like?"
          value={choice}
          options={FORMAT_OPTIONS}
          onValueChange={(next) => {
            if (next === 'auto') {
              setCustomOpen(false);
              onFormatChange('');
              return;
            }
            if (next === 'custom') {
              setCustomOpen(true);
              onFormatChange(format);
              return;
            }
            setCustomOpen(false);
            onFormatChange(next);
          }}
        />
      </div>
      {choice === 'custom' && (
        <div className="action-advanced-body clean-dates-custom-format">
          <label className="form-label" htmlFor="clean-dates-format-input">
            strftime pattern
          </label>
          <input
            id="clean-dates-format-input"
            type="text"
            className="form-input"
            data-testid="clean-dates-format-input"
            placeholder="e.g. %m/%d/%Y"
            value={format}
            onChange={(event) => onFormatChange(event.target.value)}
          />
          <p className="form-hint">
            Values are parsed strictly against this exact pattern; anything that doesn’t match
            goes to review.
          </p>
        </div>
      )}
    </div>
  );
}
