import { useEffect, useRef } from 'react';
import { transcribeOptionsForEngine } from '../../actions/transcribeEngineCatalog';
import { EngineLanguageControl } from './EngineLanguageControl';
import { TranscribeDiarizationControl } from './TranscribeDiarizationControl';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

const SPEAKER_FIELDS = ['diarize', 'num_speakers', 'min_speakers', 'max_speakers'] as const;
const TRANSCRIBE_OPTIONS = ['language', 'model_size', 'context', 'vad', 'clean', ...SPEAKER_FIELDS] as const;

export function OcrParamsBody({ params, setParams, engine, Field }:
  GeneratedActionParamsBodyProps<'media.ocr'>) {
  const previousEngine = useRef(engine?.id);
  useEffect(() => {
    if (previousEngine.current === engine?.id) return;
    previousEngine.current = engine?.id;
    if (engine?.language?.mode === 'fixed' && params.language
      && params.language !== engine.language.fixed_language) {
      const next = { ...params };
      delete next.language;
      setParams(next);
    }
  }, [engine, params, setParams]);
  return <>
    <Field name="source" label="Image or document column" />
    <Field name="engine" />
    {engine?.language ? <EngineLanguageControl declaration={engine.language}
      value={typeof params.language === 'string' ? params.language : ''} fixedNoun="reads" testIdPrefix="ocr"
      onChange={(language) => setParams({ ...params, language: language || null })} />
      : <Field name="language" />}
    <Field name="dpi" label="PDF render resolution (DPI)" />
    <Field name="searchable_pdf" label="Create searchable PDF" />
    <p className="form-hint">Extracted text and text blocks are saved separately.
      Searchable PDFs require an engine that returns text geometry.</p>
  </>;
}

export function TranscribeParamsBody({ params, setParams, engine, Field }:
  GeneratedActionParamsBodyProps<'media.transcribe'>) {
  const support = transcribeOptionsForEngine(engine);
  const previousEngine = useRef(engine?.id);
  useEffect(() => {
    if (previousEngine.current === engine?.id) return;
    previousEngine.current = engine?.id;
    // A deliberate engine switch starts with its defaults. Never rewrite a
    // saved request on mount, nor carry another engine's unsupported knobs.
    const next = { ...params };
    for (const name of TRANSCRIBE_OPTIONS) delete next[name];
    setParams(next);
  }, [engine, params, setParams]);
  const speakerParams: Record<string, string> = {};
  for (const name of SPEAKER_FIELDS) {
    const value = params[name];
    if (value != null) speakerParams[name] = String(value);
  }
  return <>
    <Field name="source" label="Audio or video column" />
    <Field name="engine" />
    <EngineLanguageControl declaration={engine?.language}
      value={Array.isArray(params.language) ? params.language.join(', ') : ''} onChange={(language) => setParams({
        ...params, language: language.split(',').map((code) => code.trim()).filter(Boolean),
      })} />
    <TranscribeDiarizationControl key={params.engine} declaration={engine?.diarization}
      params={speakerParams} onParamsChange={(update) => {
        const values = typeof update === 'function' ? update(speakerParams) : update;
        const next = { ...params };
        for (const name of SPEAKER_FIELDS) delete next[name];
        if (values.diarize !== undefined) next.diarize = values.diarize === 'true';
        for (const name of ['num_speakers', 'min_speakers', 'max_speakers'] as const) {
          if (values[name]?.trim()) next[name] = Number(values[name]);
        }
        setParams(next);
      }} />
    {support.model_size && <Field name="model_size" />}
    {support.vad && <Field name="vad" />}
    {support.context && <Field name="context" />}
    {support.clean && <Field name="clean" />}
    <p className="form-hint" data-testid="transcribe-output-summary">
      Writes transcript text and timestamped segments
      {engine?.language?.detects && <>, plus detected_language</>}. Each output can be named independently.
    </p>
  </>;
}
