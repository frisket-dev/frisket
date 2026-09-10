import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export function LinkTableParamsBody({ params, setParams, Field }:
  GeneratedActionParamsBodyProps<'derive.link_table'>) {
  return <>
    <label className="field-group">
      <span className="form-label">Semantic join receipt</span>
      <input className="form-input" data-testid="link-table-receipt"
        value={params.source?.receipt_id ?? ''}
        onChange={(event) => setParams({ ...params,
          source: { kind: 'semantic_join', receipt_id: event.target.value },
        })} />
    </label>
    <p className="form-hint">Creates a link table from a completed semantic join receipt.
      Matching is not run again.</p>
    <Field name="include_unmatched" />
  </>;
}
