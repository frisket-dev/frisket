import { useState } from 'react';

import type { GeneratedActionCatalogEntry, GeneratedActionDraft, OutputField, DeriveCompositeRequest } from '../../api/open';
import { compatibleSourceColumns, generatedActionTemplateFromCatalogEntry } from '../../actions/model';
import { SegmentedToggle } from '../PanelPrimitives';
import { GeneratedActionForm, type GeneratedActionFormProps } from './GeneratedActionForm';
import { dedupeDefaultColumnName } from './formControlHelpers';

/** The existing extract-then-materialize command uses ordinary typed forms for
 * both steps. The run host confirms the paid extraction step. */
type DeriveActionFormProps = GeneratedActionFormProps & {
    extractEntry?: GeneratedActionCatalogEntry;
    onCompositeRun(request: DeriveCompositeRequest): void;
};

export function DeriveActionForm(props: DeriveActionFormProps) {
  if (props.catalogEntry.kind !== 'derive.table_from_list' || props.initialDraft) {
    return <GeneratedActionForm {...props} />;
  }
  if (!props.sheet) return <>
    <p className="form-hint">Generate with AI needs a source sheet. Choose a sheet to extract a new list.</p>
    <GeneratedActionForm {...props} />
  </>;
  return <DeriveFromSheetForm {...props} sheet={props.sheet} />;
}

function DeriveFromSheetForm({ extractEntry, onCompositeRun, ...props }:
  DeriveActionFormProps & { sheet: NonNullable<GeneratedActionFormProps['sheet']> }) {
  const [mode, setMode] = useState<'column' | 'ai'>(() => props.initialDraft
    || props.sheet.columns.some((column) => column.type === 'json') ? 'column' : 'ai');
  const [sheetName, setSheetName] = useState('Derived');
  const [problem, setProblem] = useState<string | null>(null);
  const extractTemplate = extractEntry && generatedActionTemplateFromCatalogEntry(extractEntry);
  const [initialExtract] = useState<GeneratedActionDraft>(() => {
    const requirement = extractEntry?.ui_hints.source_requirements?.find((item) => item.param === 'source') ?? null;
    const eligible = compatibleSourceColumns(props.sheet.columns, requirement);
    const source = eligible.find((column) => column.name === props.initialSourceColumn) ?? eligible[0];
    return {
      action_id: 'map.extract',
      scope: { kind: 'sheet_rows', sheet_id: Number(props.sheet.id),
        ...(props.hasExactRowScopeInitializer ? { row_ids: (props.selectedRowIds ?? []).map(Number) } : {}) },
      params: { source: source ? [source.name] : [], instruction: props.initialPrompt ?? '',
        fields: [{ name: 'items', type: 'list', description: 'Items to turn into rows', items: { type: 'string' } }] },
      output_names: { items: dedupeDefaultColumnName('items', props.sheet.columns) },
    };
  });
  return <>
    <SegmentedToggle ariaLabel="Derive list source mode" value={mode}
      onValueChange={(value) => setMode(value as 'column' | 'ai')}
      buttonTestId={(value) => `derive-source-mode-${value}`}
      options={[{ value: 'ai', label: 'Generate with AI', disabledReason: !extractTemplate ? 'Extraction is unavailable' : undefined },
        { value: 'column', label: 'From existing list column' }]} />
    {mode === 'column' ? <GeneratedActionForm {...props} /> : extractEntry && extractTemplate ? <>
      <label className="form-label" htmlFor="derive-target-sheet">New sheet name</label>
      <input id="derive-target-sheet" className="form-input" value={sheetName}
        onChange={(event) => setSheetName(event.target.value)} />
      <GeneratedActionForm {...props} catalogEntry={extractEntry} actionTemplate={extractTemplate}
        initialDraft={initialExtract} title="Generate a table" onExecute={(request, intent, done) => {
          if (intent === 'preview') { props.onExecute(request, intent, done); return; }
          const fields = request.params.fields as unknown as OutputField[];
          const lists = fields.filter((field) => field.type === 'list');
          if (lists.length !== 1 || !sheetName.trim()) {
            setProblem('Choose one list field and enter a name for the new sheet.');
            return;
          }
          setProblem(null);
          const itemField = lists[0].name;
          onCompositeRun({
            intent: 'derive_from_extraction', itemField,
            sheet_name: sheetName.trim(), extraction: request,
          });
        }} />
      {problem && <p className="form-error" role="alert">{problem}</p>}
    </> : <p className="form-error">Extraction is unavailable.</p>}
  </>;
}
