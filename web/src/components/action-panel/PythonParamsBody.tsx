import { useEffect, useRef } from 'react';

import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

/** Only the Params region belongs to Python; the host owns names and execution. */
export function PythonParamsBody({ params, setParams, sheet, Field }:
  GeneratedActionParamsBodyProps<'map.python'>) {
  const initialized = useRef(false);
  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    // Fresh generated fields have no schema or routes. Saved Params already own
    // both, including configurations with exclusively hidden output routes.
    if (params.return_schema && params.output_routes?.length) return;
    setParams({
      ...params,
      input_columns: params.input_columns?.length ? params.input_columns
        : (sheet?.columns ?? []).slice(0, 1).map((column) => column.name),
      code: params.code || 'result = {"computed": sum(len(str(v)) for v in row.values() if v)}',
      return_schema: { type: 'object' },
      output_routes: [{ name: 'computed', path: '$', target: { kind: 'column', type: 'json' } }],
    });
  }, [params, setParams, (sheet?.columns ?? [])]);

  return <>
    <Field name="input_columns" label="Input columns" />
    <Field name="code" label="Python snippet" />
    <details className="action-advanced">
      <summary>Return schema and output routes</summary>
      <p className="form-hint">
        Routes select values from result. Column routes appear below; named results and receipt
        evidence remain hidden. Final column names do not change route keys.
      </p>
      <Field name="return_schema" />
      <Field name="output_routes" />
    </details>
  </>;
}
