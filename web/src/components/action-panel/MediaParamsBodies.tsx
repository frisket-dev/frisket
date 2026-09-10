import { useState } from 'react';
import { PanelSelect } from '../PanelSelect';
import { looksLikeSupportedMediaUrl } from './formControlHelpers';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export function FetchUrlParamsBody({ sheet, params, Field, sampleColumnValues, onNavigateToAction }:
  GeneratedActionParamsBodyProps<'media.fetch_url'>) {
  const source = (sheet?.columns ?? []).find((column) => column.name === params.source);
  const sample = source ? sampleColumnValues?.(source.id) ?? [] : [];
  const suggestDownload = sample.length > 0
    && sample.filter(looksLikeSupportedMediaUrl).length / sample.length >= 0.5;
  return <>
    <Field name="source" label="URL column" />
    {suggestDownload && source && <div className="action-io-summary" data-testid="youtube-routing-chip">
      <p className="form-hint">"{source.name}" looks like it has supported media links — Download media
        handles those (formats, audio/video, subtitles) better than a plain download.</p>
      <button type="button" className="btn btn-secondary" data-testid="youtube-routing-navigate"
        onClick={() => onNavigateToAction?.('media.ytdlp_download', source.name)}>Use Download media</button>
    </div>}
  </>;
}

const PRESETS = [480, 720, 1080];

/** Sampling and resize controls edit semantic Params only. The common host
 * validates the actual request and owns Preview, Run, naming and lifecycle. */
export function VideoFramesParamsBody({ params, setParams, Field, errors }:
  GeneratedActionParamsBodyProps<'media.video_frames'>) {
  const sampling = params.sampling ?? { kind: 'count', count: 4 };
  const [samplingText, setSamplingText] = useState(() => String(
    sampling.kind === 'interval' ? sampling.seconds : ('count' in sampling ? sampling.count ?? 4 : 4),
  ));
  const [customDimension, setCustomDimension] = useState(false);
  const [dimensionText, setDimensionText] = useState(() => params.max_dimension == null ? '' : String(params.max_dimension));
  const dimension = params.max_dimension;
  const dimensionChoice = customDimension ? 'custom' : dimension == null ? 'original'
    : PRESETS.includes(dimension) ? String(dimension) : 'custom';
  const changeSampling = (kind: 'count' | 'interval') => {
    setSamplingText(kind === 'count' ? '4' : '5');
    setParams({ ...params, sampling: kind === 'count' ? { kind, count: 4 } : { kind, seconds: 5 } });
  };
  return <>
    <Field name="source" label="Video column" />
    <div className="action-io-summary">
      <div className="form-label">Sampling</div>
      <div className="segmented segmented-full" data-testid="frame-sampling-mode">
        <button type="button" className={sampling.kind === 'count' ? 'active' : ''}
          data-testid="frame-sampling-count" onClick={() => changeSampling('count')}>Frames per video</button>
        <button type="button" className={sampling.kind === 'interval' ? 'active' : ''}
          data-testid="frame-sampling-interval" onClick={() => changeSampling('interval')}>Every N seconds</button>
      </div>
      <div className="unit-input">
        <input data-testid={sampling.kind === 'count' ? 'field-frame_count' : 'field-interval_seconds'}
          aria-label={sampling.kind === 'count' ? 'Frames per video' : 'Seconds between sampled frames'}
          value={samplingText} onChange={(event) => {
            const raw = event.target.value;
            setSamplingText(raw);
            const value = raw.trim() && Number.isFinite(Number(raw)) ? Number(raw) : 0;
            setParams({ ...params, sampling: sampling.kind === 'count'
              ? { kind: 'count', count: value } : { kind: 'interval', seconds: value } });
          }} />
        <span className="unit-input-suffix">{sampling.kind === 'count' ? 'frames' : 'seconds'}</span>
      </div>
      {errors.sampling?.message && <p role="alert" className="form-error" data-testid="frame-sampling-error">{errors.sampling.message}</p>}
      <div className="param-row">
        <label className="form-label" htmlFor="video-frames-max-dimension">Max dimension</label>
        <PanelSelect id="video-frames-max-dimension" testId="video-frames-max-dimension-select"
          value={dimensionChoice} onValueChange={(choice) => {
            setCustomDimension(choice === 'custom');
            if (choice === 'original') {
              setDimensionText('');
              setParams({ ...params, max_dimension: null });
            } else if (choice !== 'custom') {
              setDimensionText(choice);
              setParams({ ...params, max_dimension: Number(choice) });
            } else if (dimension == null) {
              // A blank custom editor is invalid, never silent native-size intent.
              setParams({ ...params, max_dimension: 0 });
            }
          }} options={[
            { value: 'original', label: 'Original', description: 'No resizing — native frame resolution.' },
            ...PRESETS.map((value) => ({ value: String(value), label: `${value}px` })),
            { value: 'custom', label: 'Custom…' },
          ]} />
      </div>
      {dimensionChoice === 'custom' && <div className="unit-input">
        <input data-testid="field-max_dimension" aria-label="Custom max dimension in pixels"
          placeholder="e.g. 900" value={dimensionText} onChange={(event) => {
            const raw = event.target.value.replace(/[^0-9]/g, '');
            setDimensionText(raw);
            setParams({ ...params, max_dimension: raw ? Number(raw) : 0 });
          }} /><span className="unit-input-suffix">px, longest side</span>
      </div>}
      {errors.max_dimension?.message && <p role="alert" className="form-error"
        data-testid="video-frames-max-dimension-error">{errors.max_dimension.message}</p>}
      <p className="form-hint" data-testid="video-frames-max-dimension-hint">
        Downscale-only, aspect preserved. Original keeps each frame at native resolution.
      </p>
      <p className="form-hint" data-testid="action-output-summary">Output frames are image blobs with timestamp metadata.</p>
    </div>
  </>;
}
