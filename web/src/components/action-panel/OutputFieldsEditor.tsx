// Controlled output-field rendering + mutations: the "Columns it creates" /
// "Output details" builder — header row with Add column, per-field FieldRow, category labels,
// and the nested list-item shape editor. Fully controlled: ActionForm owns
// the fields/labelsRaw buffer and passes explicit functional-update
// callbacks; every mutation here is a pure transform dispatched through
// them. This module never builds an action request or decides readiness.
import { Plus, Trash2 } from 'lucide-react';
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
 * Both call sites get the calm-hierarchy skin via the `field-row-calm` class
 * (see styles.css) — applied unconditionally rather than through a prop,
 * since every FieldRow instance wants it. The CSS targets the class, never DOM
 * position, so a level can no longer silently fall out of it the way the old
 * `.fields-builder > .field-group > .field-row` chain did for nested rows.
 * `nested` opts into the smaller-scale `.list-item-field-row` modifier used
 * only by the inset "Shape of each item" rows — same structure, smaller scale.
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
  singleOutput = false,
  nested = false,
  readOnly = false,
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
  singleOutput?: boolean;
  /** Nested "Shape of each item" instance: same calm skin, smaller scale. */
  nested?: boolean;
  /** Declared, non-editable output column (plugin @plugin.op writes, core
   *  recipe output contract): name/type/description render as static text and
   *  there is no delete affordance — the action owns its outputs. */
  readOnly?: boolean;
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
  const rowClassName = [
    'field-row',
    'field-row-calm',
    nested && 'list-item-field-row',
    singleOutput && 'single-output-field-row',
    readOnly && 'field-row-readonly',
  ]
    .filter(Boolean)
    .join(' ');
  if (readOnly) {
    // The action DECLARES these columns; render them as static, non-editable
    // facts (no inputs, no delete). Keeps the same testids so specs can read
    // the resolved name/type/description without an editable affordance.
    return (
      <div className={rowClassName} data-testid={rowTestId}>
        {showName && (
          <span className="field-name field-name-readonly" data-testid={nameTestId}>
            {name}
          </span>
        )}
        {showType && (
          <span className="field-type field-type-readonly" data-testid={typeTestId}>
            {type}
          </span>
        )}
        {description ? (
          <span className="field-desc field-desc-readonly" data-testid={descTestId}>
            {description}
          </span>
        ) : null}
      </div>
    );
  }
  return (
    <div className={rowClassName} data-testid={rowTestId}>
      {showName && (
        showRequired ? (
          <span className="field-name-wrap">
            <input
              className="form-input field-name"
              data-testid={nameTestId}
              value={name}
              placeholder={namePlaceholder}
              aria-label={`${ariaLabel} name`}
              onChange={(e) => onNameChange(e.target.value)}
            />
            <button
              type="button"
              className={`field-required-toggle${required ? ' is-required' : ''}`}
              data-testid={requiredTestId}
              aria-pressed={required}
              aria-label={`${ariaLabel} required`}
              title={
                required
                  ? 'Required — the model must find this or the field is flagged'
                  : 'Optional — click to require this field'
              }
              onClick={onRequiredToggle}
            >
              *
            </button>
          </span>
        ) : (
          <input
            className="form-input field-name"
            data-testid={nameTestId}
            value={name}
            placeholder={namePlaceholder}
            aria-label={`${ariaLabel} name`}
            onChange={(e) => onNameChange(e.target.value)}
          />
        )
      )}
      {showType && (
        <PanelSelect
          className="form-input field-type"
          testId={typeTestId}
          value={type}
          ariaLabel={`${ariaLabel} type`}
          onValueChange={onTypeChange}
          options={typeOptions.map((t) => ({ value: t, label: t }))}
        />
      )}
      <textarea
        className="form-input field-desc"
        data-testid={descTestId}
        value={description}
        placeholder={descPlaceholder}
        rows={1}
        ref={resizeTextareaToContent}
        onInput={handleAutoResizeTextareaInput}
        aria-label={`${ariaLabel} description`}
        onChange={(e) => onDescChange(e.target.value)}
      />
      {showDelete && (
        <button
          type="button"
          className="icon-btn"
          data-testid={deleteTestId}
          aria-label={`Remove ${ariaLabel}`}
          disabled={deleteDisabled}
          onClick={onDelete}
        >
          <Trash2 size={13} />
        </button>
      )}
    </div>
  );
}

export function OutputFieldsEditor({
  fields,
  labelsRaw,
  actionKind,
  usesFieldNameDestination,
  outputFieldsReadOnly,
  onFieldsChange,
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
  onFieldsChange(update: (previous: EditableOutputField[]) => EditableOutputField[]): void;
  onLabelsRawChange(update: (previous: Record<string, string>) => Record<string, string>): void;
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
        <div className="form-label-actions">
          {!usesFieldNameDestination && !outputFieldsReadOnly && (maxFields === undefined || fields.length < maxFields) && (
            <button
              type="button"
              className="mini-btn"
              onClick={() => onFieldsChange((fs) => [
                ...fs,
                makeEditableField({ name: `field_${fs.length + 1}`, type: 'text', description: '' }),
              ])}
            >
              <Plus size={12} /> Add column
            </button>
          )}
        </div>
      </div>
      {outputFieldsReadOnly && (
        <p className="form-hint" data-testid="declared-outputs-hint">
          This action defines its own output columns.
        </p>
      )}
      <div className="fields-builder">
        {fields.map((f, i) => (
          <div className="field-group" key={f.uiId}>
            <FieldRow
              name={f.name}
              type={f.type}
              description={f.description}
              typeOptions={fieldTypes}
              ariaLabel={`Field ${i + 1}`}
              // Classify always writes one category column — no name/type/delete.
              showName={!usesFieldNameDestination}
              showType={!usesFieldNameDestination}
              showDelete={!usesFieldNameDestination && !outputFieldsReadOnly}
              // Extract's per-field required toggle: only where a user actually
              // defines the output fields (not classify's name-destination row,
              // not declared read-only outputs).
              showRequired={actionKind === 'map.extract' && !usesFieldNameDestination && !outputFieldsReadOnly}
              required={f.required ?? false}
              singleOutput={usesFieldNameDestination}
              readOnly={outputFieldsReadOnly}
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
                <textarea
                  className="form-input field-labels field-labels-textarea"
                  data-testid={actionKind === 'map.classify' ? 'classify-labels' : undefined}
                  value={labelsRaw[f.uiId] ?? f.labels?.join(', ') ?? ''}
                  placeholder="labels, comma-separated (e.g. corruption, transit, other / unclear)"
                  aria-label={`Field ${i + 1} labels`}
                  rows={2}
                  ref={resizeTextareaToContent}
                  onInput={handleAutoResizeTextareaInput}
                  onChange={(e) => {
                    const labels = parseCommaLabels(e.target.value);
                    const descriptions = Object.fromEntries(
                      Object.entries(f.labelDescriptions ?? {}).filter(([label]) => labels.includes(label)),
                    );
                    onLabelsRawChange((m) => ({ ...m, [f.uiId]: e.target.value }));
                    setField(i, { labels, labelDescriptions: descriptions });
                  }}
                />
                {actionKind === 'map.classify' && (f.labels?.length ?? 0) > 0 && (
                  <details
                    className="classify-label-details"
                    data-testid="classify-label-details"
                    open={Object.values(f.labelDescriptions ?? {}).some(
                      (description) => description.trim().length > 0,
                    ) || undefined}
                  >
                    <summary>More details</summary>
                    <div
                      className="label-descriptions"
                      data-testid="classify-label-descriptions"
                    >
                      <p className="form-hint">
                        Optional descriptions improve local semantic matching. Add an Other / Unclear label for ambiguous rows.
                      </p>
                      {f.labels?.map((label) => (
                        <label className="label-description-row" key={label}>
                          <span>{label}</span>
                          <input
                            className="form-input"
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
                        </label>
                      ))}
                    </div>
                  </details>
                )}
              </>
            )}
            {f.type === 'list' && !usesFieldNameDestination && !outputFieldsReadOnly && (
              <div className="list-item-schema" data-testid="list-item-schema">
                <div className="list-item-schema-header">
                  <span className="list-item-schema-title">Shape of each item</span>
                  <PanelSelect
                    className="form-input list-item-kind"
                    testId="list-item-kind"
                    ariaLabel={`Field ${i + 1} list item shape`}
                    value={f.itemMode}
                    onValueChange={(value) => setFieldItemMode(f.uiId, value as ItemMode)}
                    options={[
                      { value: 'object', label: 'Object with typed fields' },
                      ...actionListItemFieldTypes.map((t) => ({
                        value: t,
                        label: `Plain list · ${t}`,
                      })),
                    ]}
                  />
                </div>
                {f.itemMode === 'object' && (
                  <div className="list-item-fields" data-testid="list-item-fields">
                    {f.itemFields.map((item, itemIndex) => (
                      <FieldRow
                        key={item.uiId}
                        name={item.name}
                        type={item.type}
                        description={item.description}
                        typeOptions={actionListItemFieldTypes}
                        ariaLabel={`List item field ${itemIndex + 1}`}
                        namePlaceholder="field"
                        descPlaceholder="description"
                        deleteDisabled={f.itemFields.length <= 1}
                        nested
                        nameTestId="list-item-field-name"
                        typeTestId="list-item-field-type"
                        descTestId="list-item-field-description"
                        deleteTestId="list-item-field-delete"
                        onNameChange={(value) => setItemField(f.uiId, item.uiId, { name: value })}
                        onTypeChange={(value) => setItemField(f.uiId, item.uiId, { type: value as ColumnType })}
                        onDescChange={(value) => setItemField(f.uiId, item.uiId, { description: value })}
                        onDelete={() => removeItemField(f.uiId, item.uiId)}
                      />
                    ))}
                    <button
                      type="button"
                      className="mini-btn list-item-add"
                      data-testid="list-item-field-add"
                      onClick={() => addItemField(f.uiId)}
                    >
                      <Plus size={12} /> item field
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      {actionKind === 'map.extract' && (
        <p className="form-hint" data-testid="action-output-summary">
          Multiple columns are filled from one structured call per row.
        </p>
      )}
      {actionKind !== 'map.extract' && !usesFieldNameDestination && !outputFieldsReadOnly && fields.length > 1 && (
        <p className="form-hint">Multiple fields = one structured call per row, never N calls.</p>
      )}
    </>
  );
}
