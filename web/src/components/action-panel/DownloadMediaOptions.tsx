import { useState } from 'react';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { PanelSelect } from '../PanelSelect';
import { SegmentedToggle, ToggleRow } from '../PanelPrimitives';
import { MediaProxyChip } from '../MediaProxyChip';
import { LabelChipsInput } from './LabelChipsInput';

// Standard yt-dlp format-selector strings behind plain preset names. `modes`
// gates a preset's visibility to the Audio/video mode toggle above it
// (undefined = shown in both modes); 'best_available' (empty selector = send
// nothing, yt-dlp's own default) is the default preset for both modes.
// yt-dlp's own current best-format guidance is
// `bv*+ba/b` (any video-only-or-combined stream + best audio, falling back to
// a single pre-merged format) — the old `bestvideo+bestaudio/best` omits the
// `*` wildcard, which excludes formats yt-dlp can't prove are video-only and
// silently narrows the candidate set. The height-capped presets add the `?`
// operator (`height<=?N`) so a format with UNKNOWN height (some DASH streams
// don't report one) still qualifies instead of being dropped outright.
const YT_FORMAT_PRESETS: {
  value: string;
  label: string;
  selector: string;
  modes?: ('audio' | 'video')[];
}[] = [
  { value: 'best_available', label: 'Best available', selector: '' },
  { value: 'best_audio', label: 'Best audio', selector: 'bestaudio/best', modes: ['audio'] },
  { value: 'best_video', label: 'Best video', selector: 'bv*+ba/b', modes: ['video'] },
  {
    value: '480p',
    label: '480p',
    selector: 'bv*[height<=?480]+ba/b[height<=?480]',
    modes: ['video'],
  },
  {
    value: '720p',
    label: '720p',
    selector: 'bv*[height<=?720]+ba/b[height<=?720]',
    modes: ['video'],
  },
  {
    value: '1080p',
    label: '1080p',
    selector: 'bv*[height<=?1080]+ba/b[height<=?1080]',
    modes: ['video'],
  },
];

// Switching the Audio/video mode toggle used to leave
// `format_selector` exactly as-is, so "Best audio" (bestaudio/best) surviving
// a switch to Video mode would silently ship audio-only bytes labeled
// media_type: 'video' (and the inverse for a resolution-capped video preset
// switched to Audio). Only ONE of our own preset strings is remapped — a
// selector the user typed by hand into "Custom…" is never touched. A
// resolution cap (480p/720p/1080p) has no audio equivalent, so it falls back
// to "Best available" (empty selector) rather than picking an arbitrary one.
const YT_FORMAT_PRESET_MODE_COUNTERPART: Partial<Record<string, string>> = {
  best_audio: 'best_video',
  best_video: 'best_audio',
};

/** Clamps a free-text numeric field to [min, max]; blank/non-numeric input
 *  clears the field rather than clamping to a bound. */
function clampYoutubeNumeric(raw: string, min: number, max: number): string {
  const trimmed = raw.trim();
  if (!trimmed) return '';
  const value = Number(trimmed);
  if (!Number.isFinite(value)) return '';
  return String(Math.min(max, Math.max(min, value)));
}

/** The existing yt-dlp editor writes semantic typed Params; the host owns
 * source validation, materialization names and the complete run lifecycle. */
export function DownloadMediaOptions({ params, setParams, Field }:
  GeneratedActionParamsBodyProps<'media.ytdlp_download'>) {
  const opts = params.extra_opts ?? {};
  const onParamsChange = (update: (previous: typeof params) => typeof params) => setParams(update(params));
  const setOption = (name: string, value: string | number | boolean | string[] | undefined) => {
    const next = { ...opts };
    if (value === undefined) delete next[name];
    else next[name] = value;
    setParams({ ...params, extra_opts: next });
  };
  const setNumber = (name: string, raw: string, min: number, max: number) => {
    const value = clampYoutubeNumeric(raw, min, max);
    setOption(name, value === '' ? undefined : Number(value));
  };
  // Same "explicit local state wins" reasoning as ActionForm's
  // videoFramesCustomDimension — Best available's selector is '' (send
  // nothing), which is indistinguishable from a not-yet-typed Custom value if
  // derived purely from params.format_selector.
  const [youtubeFormatCustom, setYoutubeFormatCustom] = useState(false);
  return (
    <div className="action-io-summary" data-testid="youtube-media-options">
      <Field name="source" label="Source URL column" />
      <MediaProxyChip />
      {/* span, not label: SegmentedToggle's role="group" already carries
          its own aria-label below — a <label> here would claim an
          association with a single control that doesn't exist
          (react-doctor label-has-associated-control). */}
      <span className="form-label">Audio/video mode</span>
      <SegmentedToggle
        ariaLabel="Audio/video mode"
        value={params.media_type || 'video'}
        onValueChange={(next) => {
          // Decided against the CURRENT selector, not the one onParamsChange
          // eventually sees — a genuinely custom string (not one of our
          // preset strings) must keep youtubeFormatCustom untouched, so
          // this can't live inside the onParamsChange updater below.
          const currentSelector = (params.format_selector ?? '').trim();
          const matchedPreset = currentSelector
            ? YT_FORMAT_PRESETS.find((preset) => preset.selector === currentSelector)
            : undefined;
          if (matchedPreset) {
            const counterpartValue = YT_FORMAT_PRESET_MODE_COUNTERPART[matchedPreset.value];
            const counterpartPreset = counterpartValue
              ? YT_FORMAT_PRESETS.find((preset) => preset.value === counterpartValue)
              : undefined;
            onParamsChange((m) => ({ ...m, media_type: next === 'audio' ? 'audio' : 'video',
              format_selector: counterpartPreset?.selector || null }));
            // The remap above always lands on a real preset (or "Best
            // available") — a lingering Custom… override would otherwise
            // keep showing the free-text field for a value the select
            // itself now represents.
            setYoutubeFormatCustom(false);
          } else {
            onParamsChange((m) => ({ ...m, media_type: next === 'audio' ? 'audio' : 'video' }));
          }
        }}
        selectTestId="youtube-media-type-select"
        buttonTestId={(value) => `youtube-media-type-${value}`}
        options={[
          { value: 'audio', label: 'Audio' },
          { value: 'video', label: 'Video' },
        ]}
      />

      {(() => {
        // A real preset select (mapped to standard yt-dlp format-selector
        // strings) with "Custom…" revealing the free-text field, instead
        // of a bare text input. Preset visibility respects the
        // Audio/video mode toggle above (a resolution cap is meaningless
        // for an audio-only download).
        const mediaType = params.media_type === 'audio' ? 'audio' : 'video';
        const visiblePresets = YT_FORMAT_PRESETS.filter(
          (preset) => !preset.modes || preset.modes.includes(mediaType),
        );
        const currentSelector = (params.format_selector ?? '').trim();
        const matchedPreset = visiblePresets.find((preset) => preset.selector === currentSelector);
        const derivedFromValue = matchedPreset
          ? matchedPreset.value
          : currentSelector
            ? 'custom'
            : 'best_available';
        const selectValue = youtubeFormatCustom ? 'custom' : derivedFromValue;
        return (
          <>
            <div className="param-row">
              <label className="form-label" htmlFor="youtube-format-selector" title="Format">
                Format
              </label>
              <PanelSelect
                id="youtube-format-selector"
                testId="youtube-format-selector-select"
                value={selectValue}
                onValueChange={(next) => {
                  setYoutubeFormatCustom(next === 'custom');
                  if (next === 'custom') {
                    // Switching to Custom keeps whatever was already
                    // typed (or starts blank) — never silently overwrite
                    // a hand-written selector with a preset string.
                    return;
                  }
                  const preset = visiblePresets.find((candidate) => candidate.value === next);
                  onParamsChange((m) => ({ ...m, format_selector: preset?.selector || null }));
                }}
                options={[
                  ...visiblePresets.map((preset) => ({ value: preset.value, label: preset.label })),
                  { value: 'custom', label: 'Custom…' },
                ]}
              />
            </div>
            {selectValue === 'custom' && (
              <div className="param-row">
                <label className="form-label" htmlFor="youtube-format-selector-custom" title="Custom yt-dlp format">
                  yt-dlp format
                </label>
                <input
                  id="youtube-format-selector-custom"
                  className="form-input"
                  data-testid="youtube-format-selector-input"
                  value={params.format_selector ?? ''}
                  placeholder="e.g. bestvideo[height<=720]+bestaudio/best[height<=720]"
                  onChange={(e) => onParamsChange((m) => ({ ...m, format_selector: e.target.value }))}
                />
              </div>
            )}
          </>
        );
      })()}

      <details className="action-advanced" data-testid="advanced-group-yt-dlp-options">
        <summary>Advanced options</summary>
        <div className="action-advanced-body">
          <div className="toggle-row-stack">
            <ToggleRow
              title="Download subtitles"
              description="Adds caption-text and subtitle-file columns next to the media column"
              checked={opts.writesubtitles === true}
              onCheckedChange={(checked) =>
                setOption('writesubtitles', checked)
              }
              testId="youtube-adv-writesubtitles"
            />
            {/* Meaningless with subtitles off — plain JS conditional (not
                ActionParams.tsx's declarative visibleWhen: this whole panel
                is hand-written JSX, not that params-array path — see the
                "Custom…" format field above for the same idiom). */}
            {opts.writesubtitles === true && (
              <ToggleRow
                title="Include auto-generated subtitles"
                checked={opts.writeautomaticsub === true}
                onCheckedChange={(checked) =>
                  setOption('writeautomaticsub', checked)
                }
                testId="youtube-adv-writeautomaticsub"
              />
            )}
          </div>

          {opts.writesubtitles === true && (
            <>
              {/* Chips edit the typed option list, preserving unrelated saved options. */}
              <div className="form-label" id="youtube-adv-subtitleslangs-label">Subtitle languages</div>
              <LabelChipsInput
                value={Array.isArray(opts.subtitleslangs) ? opts.subtitleslangs.join(', ') : typeof opts.subtitleslangs === 'string' ? opts.subtitleslangs : ''}
                onChange={(value) => setOption('subtitleslangs', value.split(',').map((part) => part.trim()).filter(Boolean))}
                placeholder="en, es, …"
                ariaLabel="Subtitle languages"
                testId="youtube-adv-subtitleslangs"
              />
              <p className="form-hint">
                Downloaded when the source platform makes them available.
              </p>
            </>
          )}

          <div className="toggle-row-stack">
            <ToggleRow
              title="Save thumbnail"
              description="Adds an image column next to the media column"
              checked={opts.writethumbnail === true}
              onCheckedChange={(checked) =>
                setOption('writethumbnail', checked)
              }
              testId="youtube-adv-writethumbnail"
            />
            <ToggleRow
              title="Save metadata JSON"
              description="Adds a file column with yt-dlp's full video metadata"
              checked={opts.writeinfojson === true}
              onCheckedChange={(checked) =>
                setOption('writeinfojson', checked)
              }
              testId="youtube-adv-writeinfojson"
            />
          </div>

          <div className="param-row">
            <label className="form-label" htmlFor="youtube-adv-merge-output-format" title="Merge output format">
              Merge output format
            </label>
            <PanelSelect
              id="youtube-adv-merge-output-format"
              testId="youtube-adv-merge-output-format"
              value={typeof opts.merge_output_format === 'string' ? opts.merge_output_format : 'auto'}
              onValueChange={(value) =>
                setOption('merge_output_format', value === 'auto' ? undefined : value)
              }
              options={[
                { value: 'auto', label: 'Auto' },
                { value: 'mp4', label: 'mp4' },
                { value: 'mkv', label: 'mkv' },
                { value: 'webm', label: 'webm' },
              ]}
            />
          </div>

          {/* Rate limit and Socket timeout were pulled per owner request
              ("sleep interval is fine and retries is fine") — the form no
              longer renders or serializes either. Retries and Sleep interval
              stay, still sharing the dense-grid primitive (denseGroup
              pattern): compact number+unit fields, one track each, two per
              row. */}
          <div className="dense-grid" data-testid="youtube-adv-throttle-grid">
            <div className="dense-grid-item" data-span="1">
              <label className="form-label" htmlFor="youtube-adv-retries">Retries</label>
              <div className="unit-input">
                <input
                  id="youtube-adv-retries"
                  data-testid="youtube-adv-retries"
                  value={typeof opts.retries === 'number' ? opts.retries : ''}
                  placeholder="default"
                  onChange={(e) =>
                    setNumber('retries', e.target.value, 0, 20)
                  }
                />
                <span className="unit-input-suffix">attempts</span>
              </div>
            </div>

            <div className="dense-grid-item" data-span="1">
              <label className="form-label" htmlFor="youtube-adv-sleep-interval" title="Sleep interval">
                Sleep interval
              </label>
              <div className="unit-input">
                <input
                  id="youtube-adv-sleep-interval"
                  data-testid="youtube-adv-sleep-interval"
                  value={typeof opts.sleep_interval === 'number' ? opts.sleep_interval : ''}
                  placeholder="default"
                  onChange={(e) =>
                    setNumber('sleep_interval', e.target.value, 0, 60)
                  }
                />
                <span className="unit-input-suffix">seconds</span>
              </div>
            </div>
          </div>

          <p className="form-hint">
            Numbers outside the server's allowed range are clamped automatically.
          </p>
        </div>
      </details>

    </div>
  );
}
