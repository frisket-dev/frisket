// Generic catalog-parameter rendering. ActionParams receives an ALREADY-FILTERED
// parameter list: the action-kind bespoke/internal exclusions (NER, semantic/tabular
// joins, YouTube media, video frames, census, clean_dates, derive
// item_field) are computed explicitly at the call site in ActionForm and
// passed down — they never live here, and there is no kind→exclusions
// registry. The only filtering this module performs is the declarative
// per-param `visibleWhen` dependency, which needs the live values.
import { useEffect, useRef, useState, type ReactNode } from 'react';
import type {
  ActionParam,
  ActionParamUnitPresentation,
  ColumnDef,
  ParamDiagnostic,
  ParamValidationResult,
  RecipeFencePosture,
} from '../../api/open';
import { getRuntimeConfig } from '../../api/open';
import { PYTHON_SNIPPET_TRUST_COPY, asRecipeFencePosture } from '../../actions/model';
import { PanelSelect } from '../PanelSelect';
import { ToggleRow } from '../PanelPrimitives';
import { CodeEditor } from '../CodeEditor';
import { MultiColumnPicker } from '../MultiColumnPicker';
import { OutputNameCombobox } from './TargetSaveToControl';
import {
  columnOptionsForParam,
  columnSelectOptions,
  handleAutoResizeTextareaInput,
  resizeTextareaToContent,
  splitColumnParamValue,
} from './formControlHelpers';

export interface ActionParamFieldPresentationProps {
  name: string;
  label: string;
  value: string;
  onChange(value: string): void;
  id: string;
  testid: string;
}

/** Optional field-presentation seam used by the generated schema renderer to
 * reuse generic fields inside richer form layouts. Undefined leaves the
 * ordinary kind-free ParamInput path in charge; this is not catalog or plugin
 * vocabulary. */
export type ActionParamFieldPresentationRenderer = (
  props: ActionParamFieldPresentationProps,
) => ReactNode | undefined;

/** Slug a group label for use in a data-testid, e.g. 'Retrieval settings' ->
 *  'retrieval-settings'. */
function advancedGroupTestId(group: string): string {
  const slug = group.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
  return `advanced-group-${slug}`;
}

function denseGridTestId(group: string): string {
  const slug = group.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
  return `dense-grid-${slug}`;
}

/** Grid tracks a dense-grid item claims. Explicit `denseSpan` wins; otherwise
 *  wide controls (selects, column pickers) take the full row and compact ones
 *  (text/number/checkbox) pack two-up. */
function denseSpanFor(param: ActionParam): 1 | 2 {
  if (param.denseSpan) return param.denseSpan;
  if (
    param.input === 'select' ||
    param.input === 'column' ||
    param.input === 'column-optional' ||
    param.input === 'columns'
  ) {
    return 2;
  }
  return 1;
}

export function ActionParams({
  params,
  advancedTestId,
  values,
  columnValues,
  columns,
  onChange,
  diagnostics,
  renderField,
  testIdOverrides,
}: {
  /** Already filtered by the caller: params rendered bespoke elsewhere in
   *  the form (or internal-only) must not be in this list. */
  params: ActionParam[] | undefined;
  advancedTestId?: string;
  values: Record<string, string>;
  columnValues?: Readonly<Record<string, readonly string[]>>;
  columns: ColumnDef[];
  onChange: (name: string, value: string | string[]) => void;
  diagnostics?: ParamValidationResult | null;
  renderField?: ActionParamFieldPresentationRenderer;
  testIdOverrides?: Readonly<Record<string, string>>;
}) {
  const renderedBasic: ReactNode[] = [];
  const renderedAdvanced = [];
  const groupOrder: string[] = [];
  const groupedRendered = new Map<string, ReactNode[]>();
  // A run of `layout: 'grid'` booleans renders as ONE compact checkbox grid
  // (clean_column's Cleanings section). Buffer consecutive grid params
  // and flush the fieldset when a non-grid param interrupts them. The shared
  // `group` (if any) becomes the fieldset's legend.
  let gridBuffer: ReactNode[] = [];
  let gridGroup: string | undefined;
  const flushGrid = () => {
    if (!gridBuffer.length) return;
    const legend = gridGroup;
    renderedBasic.push(
      <fieldset key={`cleanings-grid-${renderedBasic.length}`} className="cleanings-grid" data-testid="cleanings-grid">
        {legend && <legend className="cleanings-grid-legend">{legend}</legend>}
        <div className="cleanings-grid-items">{gridBuffer}</div>
      </fieldset>,
    );
    gridBuffer = [];
    gridGroup = undefined;
  };
  // A run of params sharing a `denseGroup` renders as ONE auto-fit CSS grid
  // (`.dense-grid`) — unlike the checkbox-only cleanings grid above, any input
  // type packs in, each item spanning 1 or 2 tracks (denseSpanFor). Buffered
  // and flushed on interruption the same way.
  let denseBuffer: ReactNode[] = [];
  let denseGroupKey: string | undefined;
  const flushDense = () => {
    if (!denseBuffer.length) return;
    renderedBasic.push(
      <div
        key={`dense-grid-${renderedBasic.length}`}
        className="dense-grid"
        data-testid={denseGridTestId(denseGroupKey ?? '')}
      >
        {denseBuffer}
      </div>,
    );
    denseBuffer = [];
    denseGroupKey = undefined;
  };
  for (const param of params ?? []) {
    if (param.visibleWhen && values[param.visibleWhen.param] !== param.visibleWhen.value) {
      if (!param.layout) flushGrid();
      if (param.denseGroup !== denseGroupKey) flushDense();
      continue;
    }
    const rendered = (
      <ParamInput
        key={param.name}
        param={param}
        value={values[param.name] ?? ''}
        columnValue={columnValues?.[param.name]}
        columns={columns}
        onChange={(value) => onChange(param.name, value)}
        diagnostic={diagnostics?.[param.name] ?? null}
        testId={testIdOverrides?.[param.name]}
        renderField={renderField}
      />
    );
    if (param.layout === 'grid' && !param.advanced && !param.advancedGroup) {
      flushDense();
      if (param.group) gridGroup = param.group;
      gridBuffer.push(rendered);
      continue;
    }
    if (param.denseGroup && !param.advanced && !param.advancedGroup) {
      flushGrid();
      if (denseGroupKey && denseGroupKey !== param.denseGroup) flushDense();
      denseGroupKey = param.denseGroup;
      denseBuffer.push(
        <div key={param.name} className="dense-grid-item" data-span={denseSpanFor(param)}>
          <ParamInput
            param={param}
            value={values[param.name] ?? ''}
            columnValue={columnValues?.[param.name]}
            columns={columns}
            onChange={(value) => onChange(param.name, value)}
            diagnostic={diagnostics?.[param.name] ?? null}
            testId={testIdOverrides?.[param.name]}
            renderField={renderField}
            dense
          />
        </div>,
      );
      continue;
    }
    flushGrid();
    flushDense();
    if (param.advancedGroup) {
      if (!groupedRendered.has(param.advancedGroup)) {
        groupOrder.push(param.advancedGroup);
        groupedRendered.set(param.advancedGroup, []);
      }
      groupedRendered.get(param.advancedGroup)!.push(rendered);
    } else if (param.advanced) {
      renderedAdvanced.push(rendered);
    } else {
      renderedBasic.push(rendered);
    }
  }
  flushGrid();
  flushDense();
  const groupDisclosures = groupOrder.map((group) => (
    <details key={group} className="action-advanced" data-testid={advancedGroupTestId(group)}>
      <summary>{group}</summary>
      <div className="action-advanced-body">{groupedRendered.get(group)}</div>
    </details>
  ));
  if (!renderedAdvanced.length && !groupDisclosures.length) return <>{renderedBasic}</>;
  return (
    <>
      {renderedBasic}
      {groupDisclosures}
      {renderedAdvanced.length > 0 && (
        <details className="action-advanced" data-testid={advancedTestId ?? 'advanced-params'}>
          <summary>Advanced options</summary>
          <div className="action-advanced-body">{renderedAdvanced}</div>
        </details>
      )}
    </>
  );
}

const MONO_STYLE = { fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)' };

function canonicalUnitValue(displayValue: string, multiplier: number): string {
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(displayValue)) return '';
  const canonical = Number(displayValue) * multiplier;
  return Number.isSafeInteger(canonical) && canonical > 0 ? String(canonical) : '';
}

function unitDisplayValue(value: string, multiplier: number): string {
  if (!/^[1-9]\d*$/.test(value)) return value;
  const canonical = Number(value);
  return Number.isSafeInteger(canonical) ? String(canonical / multiplier) : value;
}

/** A generic local display/unit control. `value` remains the parent-owned
 * canonical positive integer; the display draft is deliberately local so a
 * unit switch merely re-expresses the same canonical value. */
export function UnitInput({
  unit,
  value,
  onChange,
  id,
  testid,
  placeholder,
  maxLength,
  ariaInvalid,
}: {
  unit: ActionParamUnitPresentation;
  value: string;
  onChange(value: string): void;
  id: string;
  testid: string;
  placeholder?: string;
  maxLength?: number;
  ariaInvalid?: true;
}) {
  const [selectedUnit, setSelectedUnit] = useState(unit.defaultUnit);
  const selected = unit.units.find((option) => option.value === selectedUnit) ?? unit.units[0];
  const [displayValue, setDisplayValue] = useState(() => unitDisplayValue(value, selected.multiplier));
  const lastWrittenCanonicalValue = useRef<string | null>(null);
  const previousMultiplier = useRef(selected.multiplier);

  useEffect(() => {
    const unitChanged = previousMultiplier.current !== selected.multiplier;
    previousMultiplier.current = selected.multiplier;
    if (unitChanged || lastWrittenCanonicalValue.current !== value) {
      setDisplayValue(unitDisplayValue(value, selected.multiplier));
    }
    lastWrittenCanonicalValue.current = null;
  }, [value, selected.multiplier]);

  return (
    <div className="unit-input">
      <input
        id={id}
        value={displayValue}
        placeholder={placeholder}
        maxLength={maxLength}
        data-testid={testid}
        aria-invalid={ariaInvalid}
        onChange={(event) => {
          const next = event.target.value;
          setDisplayValue(next);
          const canonical = canonicalUnitValue(next, selected.multiplier);
          lastWrittenCanonicalValue.current = canonical;
          onChange(canonical);
        }}
      />
      <PanelSelect
        className="unit-input-suffix"
        value={selected.value}
        data-testid={`${testid}-unit`}
        aria-label={`${testid} unit`}
        onChange={(event) => setSelectedUnit(event.target.value)}
      >
        {unit.units.map((option) => <option key={option.value} value={option.value}>{option.value}</option>)}
      </PanelSelect>
    </div>
  );
}

// jsx-no-jsx-as-prop: CodeEditor's contract/status props for the python
// param field are fully static (no dependency on any prop or state) —
// hoisted to module scope rather than useMemo'd, so they're built once ever,
// not once per ParamInput render.
const PYTHON_CODE_EDITOR_CONTRACT = (
  <>
    <span className="code-editor-badge">PYTHON</span>
    <span className="code-editor-contract-sep">·</span>
    <span className="code-editor-contract-text">
      <span className="cm-builtin">row</span> in → set{' '}
      <span className="cm-result">result</span>
    </span>
  </>
);
// What confines this snippet is a property of the SERVER, not of the catalog
// and not of this browser, so the status bar says what the server reported and
// nothing else. It has been wrong both ways: a green "● sandboxed / no network
// · no env · no keys" for a snippet ctypes could walk out of, then an
// unconditional "▲ not sandboxed" that kept warning after the Linux kernel
// fence (seccomp + Landlock) landed and made confinement real on the platform
// Frisket deploys on. tests/engine/test_sandbox_recipe_fence.py pins what
// actually holds; engine/sandbox/fence.py states the per-platform matrix this
// posture is minted from.
//
// Fail closed: until the fetch answers — and forever, if it fails, if the
// server is older than the field, or if the value is unrecognised — the
// posture is `unknown`, whose copy warns. There is no posture whose copy is
// silent, and no path where a missing answer reassures.
function useRecipeFencePosture(): RecipeFencePosture {
  const [posture, setPosture] = useState<RecipeFencePosture>('unknown');
  useEffect(() => {
    let live = true;
    getRuntimeConfig()
      .then((config) => {
        if (live) setPosture(asRecipeFencePosture(config.recipe_fence_posture));
      })
      .catch(() => {
        /* stays `unknown`, which warns */
      });
    return () => {
      live = false;
    };
  }, []);
  return posture;
}

/** The python `code` param: editor plus the trust line for this server's fence
 *  posture. Its own component so the posture hook is unconditional — ParamInput
 *  returns from a dozen branches. */
export function PythonCodeField({
  param,
  value,
  onChange,
  testid,
}: {
  param: ActionParam;
  value: string;
  onChange(v: string): void;
  testid: string;
}) {
  const posture = useRecipeFencePosture();
  const copy = PYTHON_SNIPPET_TRUST_COPY[posture];
  const status = (
    <>
      <span
        className={posture === 'enforced' ? 'code-editor-status-note' : 'code-editor-status-warn'}
        data-testid="python-editor-trust-warning"
        data-posture={posture}
      >
        {copy.badge}
      </span>
      <span className="code-editor-status-muted">{copy.detail}</span>
    </>
  );
  return (
    <>
      <div className="python-editor-head">
        <span className="form-label">{param.label}</span>
        <span className="python-editor-scope">runs on each row</span>
      </div>
      <CodeEditor
        value={value}
        onValueChange={onChange}
        language="python"
        ariaLabel={param.label}
        textareaTestId={testid}
        editorTestId="python-code-editor"
        contract={PYTHON_CODE_EDITOR_CONTRACT}
        status={status}
      />
      <p className="form-hint">{copy.hint}</p>
    </>
  );
}

/** One op-specific parameter (ActionTemplate.params): a text input, textarea,
 *  or column picker, exposed to tests as `field-<param name>`. */
export function ParamInput({
  param,
  value,
  columnValue,
  columns,
  onChange,
  diagnostic,
  testId,
  dense = false,
  renderField,
}: {
  param: ActionParam;
  value: string;
  columnValue?: readonly string[];
  columns: ColumnDef[];
  onChange(v: string | string[]): void;
  diagnostic?: ParamDiagnostic | null;
  testId?: string;
  /** Rendered inside a `.dense-grid-item`: label stacks above the control and
   *  the `.param-row` wrapper is dropped so the item packs into a grid cell. */
  dense?: boolean;
  renderField?: ActionParamFieldPresentationRenderer;
}) {
  const id = `param-${param.name}`;
  const testid = testId ?? `field-${param.name}`;
  // Server-truthful inline error (the real Python message + position) under
  // both ordinary and locally presented fields.
  const paramError = diagnostic && diagnostic.ok === false
    ? (
        <p className="form-error" data-testid={`${testid}-error`} role="alert">
          {diagnostic.message ?? 'Invalid value'}
          {diagnostic.position !== undefined ? ` (at position ${diagnostic.position})` : ''}
        </p>
      )
    : null;
  const presentedField = renderField?.({
    name: param.name,
    label: param.label,
    value,
    onChange: (next) => onChange(next),
    id,
    testid,
  });
  if (presentedField !== undefined) return <>{presentedField}{paramError}</>;
  const style = param.monospace ? MONO_STYLE : undefined;
  const columnOptions =
    param.input === 'column' || param.input === 'column-optional' || param.input === 'columns'
      ? columnOptionsForParam(param, columns)
      : [];
  if (param.name === 'output_name') {
    return (
      <OutputNameCombobox
        columns={columns}
        value={value}
        onChange={onChange}
        label={param.label}
        inputTestId={testid}
      />
    );
  }
  if (param.input === 'checkbox') {
    const checked = value === '' ? param.defaultValue === 'true' : value === 'true';
    // A dense-grid checkbox packs like the grid variant — a bare compact box
    // with the description on a `title` tooltip, not a tall toggle-row card.
    if (dense) {
      return (
        <label className="form-check" htmlFor={id} title={param.hint}>
          <input
            id={id}
            type="checkbox"
            checked={checked}
            data-testid={testid}
            onChange={(e) => onChange(e.target.checked ? 'true' : 'false')}
          />
          {param.label}
        </label>
      );
    }
    // Grid-layout booleans (clean_column's Cleanings) stay a bare compact
    // checkbox even when described — the description becomes a `title` tooltip
    // so a dozen of them fit a multi-column grid without a scroll.
    if (param.layout === 'grid') {
      return (
        <label className="form-check cleanings-grid-item" htmlFor={id} title={param.hint}>
          <input
            id={id}
            type="checkbox"
            checked={checked}
            data-testid={testid}
            onChange={(e) => onChange(e.target.checked ? 'true' : 'false')}
          />
          {param.label}
        </label>
      );
    }
    if (param.hint) {
      return (
        <ToggleRow
          title={param.label}
          description={param.hint}
          checked={checked}
          testId={testid}
          onCheckedChange={(next) => onChange(next ? 'true' : 'false')}
        />
      );
    }
    return (
      <label className="form-check" htmlFor={id}>
        <input
          id={id}
          type="checkbox"
          checked={checked}
          data-testid={testid}
          onChange={(e) => onChange(e.target.checked ? 'true' : 'false')}
        />
        {param.label}
      </label>
    );
  }
  // Every compact single-line widget (text/select/number/column) renders
  // label left, input right via .param-row; textarea and the chip-list
  // `columns` picker are the exceptions (multi-line-ish, potentially many
  // wrapped chips) and stay stacked, returning early instead. `columns` used
  // to fall through into .param-row's 38%/62% split regardless — squeezing
  // MultiColumnPicker's chip flow into a narrow right-hand track (~235px
  // inside the fixed 400px drawer) instead of the full field width, so 40+
  // chips wrapped near-one-per-line and the whole action panel scrolled to
  // show them. The `dense-grid` packed layout (below, via denseGroup) never
  // hit this — its label already stacks above the control for every input
  // kind — this early return only fixes the .param-row path, matching how
  // DeriveJoinForm's/the semantic-join form's own column pickers were
  // already hand-written (plain `.form-label` span + MultiColumnPicker,
  // never inside .param-row).
  if (param.input === 'textarea') {
    return (
      <>
        <label className="form-label" htmlFor={id}>{param.label}</label>
        <textarea
          id={id}
          className="form-input form-textarea form-textarea-autogrow"
          ref={resizeTextareaToContent}
          rows={4}
          style={style}
          value={value}
          placeholder={param.placeholder}
          maxLength={param.maxLength}
          data-testid={testid}
          onChange={(e) => onChange(e.target.value)}
          onInput={handleAutoResizeTextareaInput}
        />
        {paramError}
        {param.hint && <p className="form-hint">{param.hint}</p>}
      </>
    );
  }
  if (param.input === 'columns') {
    return (
      <>
        {/* span, not label: MultiColumnPicker's role="group" already carries
            its own aria-label (react-doctor label-has-associated-control) —
            same pairing ActionForm.tsx's semantic-join carry-columns field
            uses. */}
        <span className="form-label" title={param.label}>{param.label}</span>
        <MultiColumnPicker
          testId={testid}
          ariaLabel={param.label}
          value={columnValue ? [...columnValue] : splitColumnParamValue(value)}
          options={columnOptions.map((c) => ({ name: c.name, type: c.type, ai: Boolean(c.ai) }))}
          onValueChange={(names) => onChange(names)}
        />
        {param.hint && <p className="form-hint">{param.hint}</p>}
      </>
    );
  }
  const control =
    param.input === 'column' || param.input === 'column-optional' ? (
      <PanelSelect
        id={id}
        testId={testid}
        value={value}
        onValueChange={onChange}
        emptyMessage={param.aiGeneratedOnly ? 'No AI columns yet — run an AI action first.' : undefined}
        options={[
          ...(param.input === 'column-optional' ? [{ value: '', label: '— all rows —' }] : []),
          ...columnSelectOptions(columnOptions),
        ]}
      />
    ) : param.input === 'select' ? (
      <PanelSelect
        id={id}
        testId={testid}
        value={value}
        onValueChange={onChange}
        options={[
          ...(param.required && !param.defaultValue ? [{ value: '', label: 'Select…' }] : []),
          ...(param.choices ?? []).map((choice) => {
            const friendly = param.choiceOptions?.find((option) => option.value === choice);
            return {
              value: choice,
              label: friendly?.label ?? choice,
              description: friendly?.description,
            };
          }),
        ]}
      />
    ) : param.unit ? (
      <UnitInput
        unit={param.unit}
        value={value}
        onChange={onChange}
        id={id}
        testid={testid}
        placeholder={param.placeholder}
        maxLength={param.maxLength}
        ariaInvalid={paramError ? true : undefined}
      />
    ) : (
      <input
        id={id}
        className="form-input"
        style={style}
        value={value}
        placeholder={param.placeholder}
        maxLength={param.maxLength}
        data-testid={testid}
        aria-invalid={paramError ? true : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  // Dense mode drops the `.param-row` grid so the label stacks above its
  // control inside the packed `.dense-grid-item` cell.
  if (dense) {
    return (
      <>
        <label className="form-label" htmlFor={id} title={param.label}>{param.label}</label>
        {control}
        {paramError}
        {param.hint && <p className="form-hint">{param.hint}</p>}
      </>
    );
  }
  return (
    <>
      <div className="param-row">
        <label className="form-label" htmlFor={id} title={param.label}>{param.label}</label>
        {control}
      </div>
      {paramError}
      {param.hint && <p className="form-hint">{param.hint}</p>}
    </>
  );
}
