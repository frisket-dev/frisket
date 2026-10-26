import '../PanelPrimitives.css';
import '../ActionPanel.css';
import { Button } from '../Button';
import { FormField, TextInput, TextArea } from '../FormControls';
import '../EditorRows.css';
import './OutputFieldsEditor.css';
// Controlled output-field rendering + mutations: the "Columns it creates" /
// "Output details" builder — header row with Add column, per-field FieldRow, category labels,
// and the nested list-item shape editor. Fully controlled: ActionForm owns
// the fields/labelsRaw buffer and passes explicit functional-update
// callbacks; every mutation here is a pure transform dispatched through
// them. This module never builds an action request or decides readiness.
import { Plus, Trash2 } from 'lucide-react';
import { IconButton } from '../IconButton';
import type { ActionKind, ColumnType, OutputField } from '../../api/open';
import { PanelSelect } from '../PanelSelect';
import {
  actionFieldTypes,
  actionListItemFieldTypes,
} from '../../actions/model';
import {
  handleAutoResizeTextareaInput,
  parseCommaLabels,
  resizeTextareaToContent,
} from './formControlHelpers';
import {
  makeEditableField,
  newItemFieldUiId,
  patchOutputField,
  type EditableItemField,
  type EditableOutputField,
  type ItemMode,
} from './outputFieldModel';

/**
 * One editable typed field: name · type · description (+ delete). Shared by the
 * top-level "Columns it creates" builder and the nested list-item shape so both
 * read and behave identically. Any part can be hidden for single-output actions.
 *
 * Nested and top-level fields use the same controls and row presentation.
 */
function FieldRow({
  name,
  type,
  description,
  typeOptions,
  ariaLabel,
  namePlaceholder = 'name',
  descPlaceholder = 'describe what to extract',
  showName = true,
  showType = true,
  showDelete = true,
  showRequired = false,
  required = false,
  readOnly = false,
  structureLocked = false,
  lockReason,
  deleteDisabled = false,
  rowTestId,
  nameTestId,
  typeTestId,
  descTestId,
  deleteTestId,
  requiredTestId,
  onNameChange,
  onTypeChange,
  onDescChange,
  onRequiredToggle,
  onDelete,
}: {
  name: string;
  type: string;
  description: string;
  typeOptions: readonly string[];
  ariaLabel: string;
  namePlaceholder?: string;
  descPlaceholder?: string;
  showName?: boolean;
  showType?: boolean;
  showDelete?: boolean;
  /** Extract: render the required (*) toggle after the field name. */
  showRequired?: boolean;
  required?: boolean;
  /** Declared, non-editable output column (plugin @plugin.op writes, core
   *  recipe output contract): name/type/description render as static text and
   *  there is no delete affordance — the action owns its outputs. */
  readOnly?: boolean;
  /** Keep the typed output shape fixed while descriptions remain editable. */
  structureLocked?: boolean;
  lockReason?: string;
  deleteDisabled?: boolean;
  rowTestId?: string;
  nameTestId?: string;
  typeTestId?: string;
  descTestId?: string;
  deleteTestId?: string;
  requiredTestId?: string;
  onNameChange(value: string): void;
  onTypeChange(value: string): void;
  onDescChange(value: string): void;
  onRequiredToggle?(): void;
  onDelete?(): void;
}) {
  if (readOnly) {
    // The action DECLARES these columns; render them as static, non-editable
    // facts (no inputs, no delete). Keeps the same testids so specs can read
    // the resolved name/type/description without an editable affordance.
    return (
      <div className="output-field-facts" data-testid={rowTestId}>
        {showName && (
          <strong data-testid={nameTestId}>
            {name}
          </strong>
        )}
        {showType && (
          <code data-testid={typeTestId}>
            {type}
          </code>
        )}
        {description ? (
          <span data-testid={descTestId}>
            {description}
          </span>
        ) : null}
      </div>
    );
  }
  return (
    <div className="content-stack" data-testid={rowTestId}>
      {(showName || showType || showDelete) && <div className="editor-row-controls">
      {showName && <span className="output-field-name">
            <TextInput
              data-testid={nameTestId}
              value={name}
              placeholder={namePlaceholder}
              aria-label={`${ariaLabel} name`}
              disabled={structureLocked}
              title={structureLocked ? lockReason : undefined}
              onChange={(e) => onNameChange(e.target.value)}
            />
            {showRequired && <Button
              type="button"
              variant="text"
              className="btn-icon output-field-required"
              data-testid={requiredTestId}
              aria-pressed={required}
              aria-label={`${ariaLabel} required`}
              disabled={structureLocked}
              title={
                structureLocked
                  ? lockReason
                  : required
                  ? 'Required — the model must find this or the field is flagged'
                  : 'Optional — click to require this field'
              }
              onClick={onRequiredToggle}
            >
              *
            </Button>}
          </span>}
      {showType && <span className="output-field-type">
        <PanelSelect
          testId={typeTestId}
          value={type}
          ariaLabel={`${ariaLabel} type`}
          disabled={structureLocked}
          title={structureLocked ? lockReason : undefined}
          onValueChange={onTypeChange}
          options={typeOptions.map((t) => ({ value: t, label: t }))}
        />
      </span>}
      {showDelete && (
        <IconButton
          icon={Trash2}
          data-testid={deleteTestId}
          label={`Remove ${ariaLabel}`}
          disabled={deleteDisabled || structureLocked}
          title={structureLocked ? lockReason : undefined}
          onClick={onDelete}
        />
      )}
      </div>}
      <TextArea
        className="output-field-description"
        data-testid={descTestId}
        value={description}
        placeholder={descPlaceholder}
        rows={1}
        ref={resizeTextareaToContent}
        onInput={handleAutoResizeTextareaInput}
        aria-label={`${ariaLabel} description`}
        onChange={(e) => onDescChange(e.target.value)}
      />
    </div>
  );
}

export function OutputFieldsEditor({
  fields,
  labelsRaw,
  actionKind,
  usesFieldNameDestination,
  outputFieldsReadOnly,
  structureLocked = false,
  lockReason = 'Fixed for this comparison.',
  onFieldsChange, labelError, onLabelInteraction,
  onLabelsRawChange,
  fieldTypes = actionFieldTypes,
  maxFields,
  minFields = 1,
}: {
  fields: EditableOutputField[];
  /** Per-field raw labels text as typed (uiId-keyed), so "a, , b," round-trips
   *  through the textarea while the parsed labels live on the field. */
  labelsRaw: Record<string, string>;
  actionKind: ActionKind;
  /** Classify: one category column whose NAME is the destination — the row
   *  hides name/type/delete and the labels textarea is the whole editor. */
  usesFieldNameDestination: boolean;
  /** Declared (non-user-defined) output columns render read-only. */
  outputFieldsReadOnly: boolean;
  /** Comparison/Replace freezes names, types, labels, and nested schema. */
  structureLocked?: boolean;
  lockReason?: string;
  onFieldsChange(update: (previous: EditableOutputField[]) => EditableOutputField[]): void;
  onLabelsRawChange(update: (previous: Record<string, string>) => Record<string, string>): void;
  /** Classify routes a nested server path to one labels textarea. */
  labelError?: { fieldIndex: number; message: string };
  onLabelInteraction?(fieldIndex: number): void;
  fieldTypes?: readonly string[];
  maxFields?: number;
  minFields?: number;
}) {
  const setField = (i: number, patch: Partial<OutputField>) =>
    onFieldsChange((fs) => patchOutputField(fs, i, patch));
  const setFieldItemMode = (fieldUiId: string, itemMode: ItemMode) =>
    onFieldsChange((fs) => fs.map((field) => {
      if (field.uiId !== fieldUiId) return field;
      return {
        ...field,
        itemMode,
        itemFields: itemMode === 'object' && field.itemFields.length === 0
          ? [
              {
                uiId: newItemFieldUiId(),
                name: 'value',
                type: 'text',
                description: '',
              },
            ]
          : field.itemFields,
      };
    }));
  const setItemField = (
    fieldUiId: string,
    itemUiId: string,
    patch: Partial<Omit<EditableItemField, 'uiId'>>,
  ) =>
    onFieldsChange((fs) => fs.map((field) => {
      if (field.uiId !== fieldUiId) return field;
      return {
        ...field,
        itemFields: field.itemFields.map((item) => (
          item.uiId === itemUiId ? { ...item, ...patch } : item
        )),
      };
    }));
  const addItemField = (fieldUiId: string) =>
    onFieldsChange((fs) => fs.map((field) => (
      field.uiId === fieldUiId
        ? {
            ...field,
            itemMode: 'object',
            itemFields: [
              ...field.itemFields,
              {
                uiId: newItemFieldUiId(),
                name: `field_${field.itemFields.length + 1}`,
                type: 'text',
                description: '',
              },
            ],
          }
        : field
    )));
  const removeItemField = (fieldUiId: string, itemUiId: string) =>
    onFieldsChange((fs) => fs.map((field) => (
      field.uiId === fieldUiId
        ? {
            ...field,
            itemFields: field.itemFields.filter((item) => item.uiId !== itemUiId),
          }
        : field
    )));

  return (
    <>
      <div className="form-label-row">
        <span className="form-label" data-testid="output-fields-label">
          {usesFieldNameDestination ? 'Output details' : 'Columns it creates'}
        </span>
        <div className="control-row">
          {!usesFieldNameDestination && !outputFieldsReadOnly && !structureLocked
            && (maxFields === undefined || fields.length < maxFields) && (
            <Button
              type="button"

              onClick={() => onFieldsChange((fs) => [
                ...fs,
                makeEditableField({
                  name: `field_${fs.length + 1}`,
                  type: (fieldTypes?.[0] ?? 'text') as ColumnType,
                  description: '',
                }),
              ])}
            >
              <Plus size={12} /> Add column
            </Button>
          )}
        </div>
      </div>
      {outputFieldsReadOnly && (
        <p className="form-hint" data-testid="declared-outputs-hint">
          This action defines its own output columns.
        </p>
      )}
      <div className="editor-list" data-testid="output-fields-editor">
        {fields.map((f, i) => (
          <div className="editor-row" key={f.uiId}>
            <FieldRow
              name={f.name}
              type={f.type}
              description={f.description}
              typeOptions={fieldTypes}
              ariaLabel={`Field ${i + 1}`}
              // Classify always writes one category column — no name/type/delete.
              showName={!usesFieldNameDestination}
              showType={!usesFieldNameDestination}
              showDelete={!usesFieldNameDestination && !outputFieldsReadOnly && !structureLocked}
              // Extract's per-field required toggle: only where a user actually
              // defines the output fields (not classify's name-destination row,
              // not declared read-only outputs).
              showRequired={actionKind === 'map.extract' && !usesFieldNameDestination && !outputFieldsReadOnly}
              required={f.required ?? false}
              readOnly={outputFieldsReadOnly}
              structureLocked={structureLocked}
              lockReason={lockReason}
              deleteDisabled={fields.length <= minFields}
              rowTestId="output-field-row"
              nameTestId="output-field-name"
              typeTestId="output-field-type"
              descTestId="output-field-description"
              deleteTestId="output-field-delete"
              requiredTestId="output-field-required"
              onNameChange={(value) => setField(i, { name: value })}
              onTypeChange={(value) => setField(i, { type: value as ColumnType })}
              onDescChange={(value) => setField(i, { description: value })}
              onRequiredToggle={() => setField(i, { required: !(f.required ?? false) })}
              onDelete={() => onFieldsChange((fs) => fs.filter((field) => field.uiId !== f.uiId))}
            />
            {f.type === 'category' && !outputFieldsReadOnly && (
              <>
                <TextArea
                  className="output-field-description"
                  data-testid={actionKind === 'map.classify' ? 'classify-labels' : undefined}
                  value={labelsRaw[f.uiId] ?? f.labels?.join(', ') ?? ''}
                  placeholder="labels, comma-separated (e.g. corruption, transit, other / unclear)"
                  aria-label={`Field ${i + 1} labels`}
                  aria-describedby={labelError?.fieldIndex === i ? `classify-labels-error-${f.uiId}` : undefined}
                  rows={2}
                  disabled={structureLocked}
                  title={structureLocked ? lockReason : undefined}
                  ref={resizeTextareaToContent}
                  onInput={handleAutoResizeTextareaInput}
                  onFocus={() => onLabelInteraction?.(i)}
                  onChange={(e) => {
                    const labels = parseCommaLabels(e.target.value);
                    const descriptions = Object.fromEntries(
                      Object.entries(f.labelDescriptions ?? {}).filter(([label]) => labels.includes(label)),
                    );
                    onLabelsRawChange((m) => ({ ...m, [f.uiId]: e.target.value }));
                    setField(i, { labels, labelDescriptions: descriptions });
                  }}
                />
                {labelError?.fieldIndex === i && (
                  <p className="panel-error" role="alert" data-testid="classify-labels-error"
                    id={`classify-labels-error-${f.uiId}`}>{labelError.message}</p>
                )}
                {actionKind === 'map.classify' && (f.labels?.length ?? 0) > 0 && (
                  <details
                    className="action-advanced"
                    data-testid="classify-label-details"
                    open={Object.values(f.labelDescriptions ?? {}).some(
                      (description) => description.trim().length > 0,
                    ) || undefined}
                  >
                    <summary>More details</summary>
                    <div
                      className="content-stack"
                      data-testid="classify-label-descriptions"
                    >
                      <p className="form-hint">
                        Optional descriptions improve local semantic matching. Add an Other / Unclear label for ambiguous rows.
                      </p>
                      {f.labels?.map((label) => (
                        <FormField label={label} layout="inline" key={label}>

                          <TextInput
                            value={f.labelDescriptions?.[label] ?? ''}
                            placeholder={`What ${label} means`}
                            aria-label={`${label} semantic description`}
                            onChange={(event) => {
                              const next = { ...(f.labelDescriptions ?? {}) };
                              const description = event.target.value;
                              if (description.trim()) next[label] = description;
                              else delete next[label];
                              setField(i, { labelDescriptions: next });
                            }}
                          />
                        </FormField>
                      ))}
                    </div>
                  </details>
                )}
              </>
            )}
            {f.type === 'list' && !usesFieldNameDestination && !outputFieldsReadOnly && (
              <div className="editor-inset" data-testid="list-item-schema">
                <FormField label="Shape of each item">
                  <PanelSelect
                    testId="list-item-kind"
                    ariaLabel={`Field ${i + 1} list item shape`}
                    value={f.itemMode}
                    disabled={structureLocked}
                    title={structureLocked ? lockReason : undefined}
                    onValueChange={(value) => setFieldItemMode(f.uiId, value as ItemMode)}
                    options={[
                      { value: 'object', label: 'Object with typed fields' },
                      ...actionListItemFieldTypes.map((t) => ({
                        value: t,
                        label: `Plain list · ${t}`,
                      })),
                    ]}
                  />
                </FormField>
                {f.itemMode === 'object' && (
                  <div className="editor-list" data-testid="list-item-fields">
                    {f.itemFields.map((item, itemIndex) => (
                      <div className="editor-row" key={item.uiId}><FieldRow
                        name={item.name}
                        type={item.type}
                        description={item.description}
                        typeOptions={actionListItemFieldTypes}
                        ariaLabel={`List item field ${itemIndex + 1}`}
                        structureLocked={structureLocked}
                        lockReason={lockReason}
                        namePlaceholder="field"
                        descPlaceholder="description"
                        deleteDisabled={f.itemFields.length <= 1}
                        nameTestId="list-item-field-name"
                        typeTestId="list-item-field-type"
                        descTestId="list-item-field-description"
                        deleteTestId="list-item-field-delete"
                        onNameChange={(value) => setItemField(f.uiId, item.uiId, { name: value })}
                        onTypeChange={(value) => setItemField(f.uiId, item.uiId, { type: value as ColumnType })}
                        onDescChange={(value) => setItemField(f.uiId, item.uiId, { description: value })}
                        onDelete={() => removeItemField(f.uiId, item.uiId)}
                      /></div>
                    ))}
                    <Button
                      type="button"
                      data-testid="list-item-field-add"
                      disabled={structureLocked}
                      title={structureLocked ? lockReason : undefined}
                      onClick={() => addItemField(f.uiId)}
                    >
                      <Plus size={12} /> item field
                    </Button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      {!structureLocked && actionKind === 'map.extract' && (
        <p className="form-hint" data-testid="action-output-summary">
          Multiple columns are filled from one structured call per row.
        </p>
      )}
      {!structureLocked && actionKind !== 'map.extract' && !usesFieldNameDestination && !outputFieldsReadOnly && fields.length > 1 && (
        <p className="form-hint">Multiple fields = one structured call per row, never N calls.</p>
      )}
    </>
  );
}
