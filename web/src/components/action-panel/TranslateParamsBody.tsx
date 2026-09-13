import { useEffect, useRef } from 'react';

import type { GeneratedActionParams } from '../../generated/actionTypes';
import { TranslatePairPicker } from '../TranslatePairPicker';
import { EngineLanguageControl } from './EngineLanguageControl';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export function TranslateParamsBody({ params, setParams, engine, errors, Field }:
  GeneratedActionParamsBodyProps<'map.translate'>) {
  const selectedEngine = params.engine ?? 'llm';
  const previousEngine = useRef(selectedEngine);
  const latestParams = useRef(params);

  useEffect(() => { latestParams.current = params; }, [params]);

  useEffect(() => {
    if (previousEngine.current === selectedEngine) return;
    previousEngine.current = selectedEngine;
    const next = { ...params };
    if (selectedEngine !== 'llm') {
      delete next.model;
      delete next.context;
    }
    if (selectedEngine === 'hy_mt2') {
      delete next.language;
      next.save_detected_language = false;
    }
    setParams(next);
  }, [params, selectedEngine, setParams]);

  // The pair swap emits two coordinated edits in one event. Merge both against
  // the latest draft so the second update retains the first language change.
  const updatePair = (change: Partial<GeneratedActionParams['map.translate']>) => {
    const next = { ...latestParams.current, ...change };
    latestParams.current = next;
    setParams(next);
  };

  return <>
    <Field name="source" label="Content" />
    <Field name="engine" />
    {selectedEngine === 'opus_mt' ? <TranslatePairPicker
      installedPairs={engine?.models ?? []}
      downloadablePairs={engine?.downloadable_pairs ?? []}
      source={params.language?.[0] ?? ''}
      target={params.target_language ?? 'English'}
      onSourceChange={(language) => updatePair({ language: language ? [language] : [] })}
      onTargetChange={(target_language) => updatePair({ target_language })}

    /> : <>
      {selectedEngine !== 'hy_mt2' && <EngineLanguageControl declaration={engine?.language}
        label="Translate from" autoLabel="Auto-detect"
        testIdPrefix="translate-source" fixedNoun="translates from"
        value={params.language?.[0] ?? ''}
        onChange={(language) => setParams({ ...params, language: language ? [language] : [] })} />}
      <Field name="target_language" label="Translate to" />
    </>}
    {errors.language?.message && <p className="form-error">{errors.language.message}</p>}
    {selectedEngine !== 'hy_mt2' && <Field name="save_detected_language" label="Save source language" />}
    {selectedEngine === 'llm' && <Field name="context" label="Dataset context" />}
    {selectedEngine === 'hy_mt2' && <p className="form-hint">
      Hy-MT2 detects the source language automatically without reporting it.
      {engine?.downloadable_model && <> The model is downloaded on first use.</>}
    </p>}
  </>;
}
