import { useEffect, useRef } from 'react';

import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { NerEngineFields } from './NerEngineFields';
import { nerLabelsOk, normalizeSpacyLabels } from './nerLabelModel';

export function NerParamsBody({
  params, setParams, setEditorProblem, errors, Field,
}: GeneratedActionParamsBodyProps<'map.ner'>) {
  const engine = params.engine ?? 'spacy';
  const threshold = useRef(params.threshold);
  const extraInstructions = useRef(params.extra_instructions);

  useEffect(() => {
    if (params.threshold != null) threshold.current = params.threshold;
    if (params.extra_instructions != null) {
      extraInstructions.current = params.extra_instructions;
    }
    const next = { ...params };
    if (engine === 'spacy' && next.labels) {
      next.labels = normalizeSpacyLabels(next.labels);
    }
    if (engine === 'gliner' && next.threshold == null && threshold.current != null) {
      next.threshold = threshold.current;
    } else if (engine !== 'gliner') delete next.threshold;
    if (engine === 'llm') {
      if (next.extra_instructions == null && extraInstructions.current != null) {
        next.extra_instructions = extraInstructions.current;
      }
    } else {
      delete next.extra_instructions;
    }
    if (JSON.stringify(next) !== JSON.stringify(params)) setParams(next);
  }, [engine, params, setParams]);

  const labelsProblem = engine === 'spacy' && (params.labels?.length ?? 0) > 0
    && !nerLabelsOk(engine, params.labels ?? [])
    ? 'Pick at least one entity type.' : null;
  useEffect(() => {
    setEditorProblem?.(labelsProblem);
    return () => setEditorProblem?.(null);
  }, [labelsProblem, setEditorProblem]);

  return <>
    <Field name="source" />
    <Field name="engine" />
    <NerEngineFields
      engine={engine}
      labels={params.labels ?? []}
      labelsError={errors.labels?.message ?? labelsProblem ?? undefined}
      onLabelsChange={(labels) => setParams({ ...params, labels })}
    />
    {engine === 'gliner' && <Field name="threshold" />}
    {engine === 'llm' && <>
      <Field name="extra_instructions" />
    </>}
  </>;
}
