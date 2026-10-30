import { useState, type ReactNode } from 'react';
import { GridCellKind, type GridCell, type GridColumn, type Item } from '@glideapps/glide-data-grid';
import type { EvaluationResponse } from '../../api/evaluation';
import type { GeneratedActionDraft } from '../../api/types';
import { Button } from '../../components/Button';
import { PanelSelect } from '../../components/PanelSelect';
import { Modal } from '../../components/Modal';
import { GridViewport } from '../../grid/GridViewport';
import type { ActionCompareVariantEditorRenderProps } from '../ActionCompareTab';
import type { ActionCompareSession, ActionCompareSessionSnapshot, ActionCompareUseSettings } from '../actionCompareSession';

export interface TrySeveralProps {
  session: ActionCompareSession;
  state: ActionCompareSessionSnapshot;
  response: EvaluationResponse | null;
  modelField: string;
  instructionsField: string;
  renderVariantEditor?: (props: ActionCompareVariantEditorRenderProps) => ReactNode;
  onOpenVariant(variantId: string): void;
  onUse(selection: ActionCompareUseSettings): void;
  onClose(): void;
  scoring?: boolean;
}

type Model = { id: number; draft: GeneratedActionDraft };
type Instructions = { id: number; value: string };
const cellKey = (model: number, instruction: number) => `${model}:${instruction}`;
const routingFields = (modelField: string) => new Set([modelField, 'model', 'engine', 'provider', 'provider_ref', 'execution_target']);
function selectedModelDraft(baseline: GeneratedActionDraft, selected: GeneratedActionDraft, modelField: string): GeneratedActionDraft {
  const params = { ...baseline.params };
  for (const key of routingFields(modelField)) {
    if (key in selected.params) params[key] = selected.params[key];
    else delete params[key];
  }
  return { ...baseline, params };
}

/** The matrix creates ordinary session variants; the session owns execution and consent. */
export function TrySeveral({ session, state, response, modelField, instructionsField,
  renderVariantEditor, onOpenVariant, onUse, onClose, scoring = false }: TrySeveralProps) {
  const baseline = state.variants[0];
  const [models, setModels] = useState<Model[]>(() => [{ id: 0, draft: baseline.draft }]);
  const [instructions, setInstructions] = useState<Instructions[]>(() => [{
    id: 0, value: String(baseline.draft.params[instructionsField] ?? ''),
  }]);
  const [nextId, setNextId] = useState(1);
  const [skipped, setSkipped] = useState<string[]>([]);
  const [created, setCreated] = useState<Record<string, string> | null>(null);
  const [field, setField] = useState('');
  const [error, setError] = useState('');
  const keys = models.flatMap(model => instructions.map(instruction => cellKey(model.id, instruction.id)));
  const selected = keys.filter(key => !skipped.includes(key));
  const writable = state.variants.filter(variant => !variant.baseline && !('readOnly' in variant && variant.readOnly)).length;
  const capacity = Math.max(0, state.context.limits.max_challengers - writable);
  const locked = created !== null || state.sequenceStatus === 'running';
  const fields = response?.link?.fields ?? [];
  const createdVariants = Object.values(created ?? {}).map(id => state.variants.find(v => v.id === id)).filter(v => v !== undefined);
  const fieldAvailable = (key: string) => {
    const definition = fields.find(candidate => candidate.logical_key === key);
    if (definition?.rule !== 'hand') return true;
    return createdVariants.length > 0 && createdVariants.every(variant => {
      const score = response?.variants.find(v => v.key === variant.displayKey)?.scores[key];
      return score !== undefined && score.remaining === 0 && score.scored > 0;
    });
  };
  const activeField = fields.some(candidate => candidate.logical_key === field && fieldAvailable(field))
    ? field : fields.find(candidate => fieldAvailable(candidate.logical_key))?.logical_key ?? '';
  const scoreFor = (id: string | undefined) => {
    const variant = state.variants.find(v => v.id === id);
    return response?.variants.find(v => v.key === variant?.displayKey)?.scores[activeField];
  };
  const createdIds = Object.values(created ?? {});
  const scores = createdIds.map(id => scoreFor(id));
  // Equal counts can still represent different rows. Compare the scored row identities.
  const scopes = createdIds.map(id => {
    const variant = state.variants.find(item => item.id === id);
    const result = response?.variants.find(item => item.key === variant?.displayKey);
    return response?.row_ids.filter(rowId => {
      const verdict = result?.verdicts[String(rowId)]?.[activeField];
      return verdict === 'match' || verdict === 'miss';
    }) ?? [];
  });
  const comparable = !scoring && scopes.length > 0 && scopes[0].length > 0
    && scores.every(score => score?.accuracy != null && score.remaining === 0)
    && scopes.every(scope => scope.length === scopes[0].length && scope.every((rowId, index) => rowId === scopes[0][index]));
  const bestScore = comparable ? Math.max(...scores.map(score => score!.accuracy!)) : null;
  const bestIds = Object.values(created ?? {}).filter(id => bestScore !== null && scoreFor(id)?.accuracy === bestScore);
  const columns: GridColumn[] = [
    { title: 'Model', id: 'model', width: 180 },
    ...instructions.map((instruction, index) => ({ title: `Instructions ${index + 1}`, id: String(instruction.id), width: 175 })),
  ];
  const getCellContent = ([column, row]: Item): GridCell => {
    const model = models[row];
    const instruction = instructions[column - 1];
    let text = '';
    if (column === 0) text = `${String(model?.draft.params[modelField] ?? '')}${modelField !== 'model' && model?.draft.params.model ? ` · ${String(model.draft.params.model)}` : ''}`;
    else if (model && instruction) {
      const key = cellKey(model.id, instruction.id);
      const id = created?.[key];
      const variant = state.variants.find(v => v.id === id);
      const score = scoreFor(id);
      text = skipped.includes(key) ? 'Skipped' : variant
        ? `${variant.displayKey} · ${score?.accuracy != null ? `${Math.round(score.accuracy * 100)}%` : variant.status}${bestIds.includes(id!) ? ' ★' : ''}`
        : 'Ready to run';
    }
    return { kind: GridCellKind.Text, data: text, displayData: text, allowOverlay: false, readonly: true };
  };
  function run() {
    if (locked || !selected.length || selected.length > capacity) return;
    // Recheck the live snapshot so a concurrent change cannot overfill the session.
    const current = session.getSnapshot();
    const used = current.variants.filter(v => !v.baseline && !('readOnly' in v && v.readOnly)).length;
    if (selected.length > current.context.limits.max_challengers - used) {
      setError('Comparison capacity changed. Remove variants or skip combinations.');
      return;
    }
    const added: Record<string, string> = {};
    for (const model of models) for (const instruction of instructions) {
      const key = cellKey(model.id, instruction.id);
      if (skipped.includes(key)) continue;
      const id = session.addVariant(baseline.id);
      if (!id) {
        Object.values(added).forEach(variantId => session.removeVariant(variantId));
        setError('Could not add all combinations. No combinations were run.');
        return;
      }
      added[key] = id;
      const draft = { ...model.draft, params: { ...model.draft.params, [instructionsField]: instruction.value } };
      if (!session.updateVariant(id, draft)) {
        Object.values(added).forEach(variantId => session.removeVariant(variantId));
        setError('Could not update all combinations. No combinations were run.');
        return;
      }
    }
    setCreated(added);
    setError('');
    void session.runPending();
  }
  return <Modal title="Try several values" onClose={onClose} closeLabel="Close try several" size="wide" testId="comparison-try-several"><section className="comparison-try-several" aria-label="Try several values">
    <p>Choose models and instructions, then run the selected combinations. This also runs other pending variants.</p>
    <fieldset disabled={locked}><legend>Models</legend>{models.map((model, index) => <div key={model.id}>
      {renderVariantEditor?.({ variantId: `try-several-model-${model.id}`, label: `Model ${index + 1}`,
        draft: model.draft, lockedParams: Object.keys(baseline.draft.params).filter(key => !routingFields(modelField).has(key)),
        structureLocked: true, lockReason: 'Fixed for this comparison',
        onDraftChange: draft => setModels(previous => previous.map(item => item.id === model.id
          ? { ...item, draft: selectedModelDraft(baseline.draft, draft, modelField) } : item)),
        onDuplicate: () => {}, onDismiss: () => {},
      })}
    </div>)}</fieldset>
    <Button type="button" disabled={locked || !renderVariantEditor} onClick={() => {
      setModels(previous => [...previous, { id: nextId, draft: baseline.draft }]); setNextId(nextId + 1);
    }}>Add model</Button>
    {instructions.map((instruction, index) => <label key={instruction.id}>Instructions {index + 1}
      <textarea aria-label={`Instructions ${index + 1}`} disabled={locked} value={instruction.value}
        onChange={event => setInstructions(previous => previous.map(item => item.id === instruction.id ? { ...item, value: event.target.value } : item))} />
    </label>)}
    <Button type="button" disabled={locked} onClick={() => {
      setInstructions(previous => [...previous, { id: nextId, value: instructions[0].value }]); setNextId(nextId + 1);
    }}>Add instructions</Button>
    {!created && <fieldset><legend>Combinations to run</legend>{models.flatMap((model, modelIndex) => instructions.map((instruction, instructionIndex) => {
      const key = cellKey(model.id, instruction.id);
      return <label key={key}><input type="checkbox" checked={!skipped.includes(key)} disabled={locked}
        onChange={event => setSkipped(previous => event.target.checked ? previous.filter(item => item !== key) : [...previous, key])} />
        Model {modelIndex + 1} · Instructions {instructionIndex + 1}</label>;
    }))}</fieldset>}
    {fields.length > 0 && <PanelSelect ariaLabel="Score on" value={activeField} onValueChange={setField}
      options={fields.map(candidate => ({ value: candidate.logical_key, label: candidate.logical_key, disabled: !fieldAvailable(candidate.logical_key) }))} />}
    <div style={{ height: Math.max(120, 40 + models.length * 42) }} data-testid="try-several-grid">
      <GridViewport columns={columns} rows={models.length} getCellContent={getCellContent}
        onCellClicked={([column, row]) => {
          const id = column > 0 && models[row] && instructions[column - 1] ? created?.[cellKey(models[row].id, instructions[column - 1].id)] : undefined;
          if (id && state.variants.find(v => v.id === id)?.status === 'ready') onOpenVariant(id);
        }} />
    </div>
    {!created && <><p>{selected.length} selected · {keys.length - selected.length} skipped</p>
      {selected.length > capacity && <p>Only {capacity} variant slots available. Skip combinations or remove variants.</p>}
      <Button type="button" disabled={locked || !renderVariantEditor || !selected.length || selected.length > capacity} onClick={run}>Run {selected.length} combinations</Button></>}
    {created && <Button type="button" disabled={!bestIds.length} onClick={() => {
      const selection = bestIds.length ? session.useVariant(bestIds[0]) : null;
      if (selection) onUse(selection);
    }}>Use best</Button>}
    {error && <p role="alert">{error}</p>}
  </section></Modal>;
}
