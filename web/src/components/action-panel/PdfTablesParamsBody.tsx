import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export function PdfTablesParamsBody({ Field }:
  GeneratedActionParamsBodyProps<'media.extract_pdf_tables'>) {
  return <>
    <Field name="source" label="PDF column" />
    <Field name="table_mode" label="Table mode" />
    <p className="form-hint">Auto lets Natural PDF choose. Stream reads whitespace-aligned tables;
      lattice follows drawn ruled lines.</p>
    <details className="action-advanced" data-testid="pdf-table-extraction-options">
      <summary>Pages and extraction options</summary>
      <Field name="extract_table" label="Extraction options" />
    </details>
  </>;
}
