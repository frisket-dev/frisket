import { Button } from '../Button';
import { FormField, TextInput } from '../FormControls';
import { useState } from 'react';
import './AskResearch.css';

export type AskResearchPendingPause =
  | {
    id: string;
    kind: 'action';
    title: string;
    effect: string;
    targets: string[];
    estimate_usd?: string | null;
    can_skip: boolean;
  }
  | {
    id: string;
    kind: 'budget';
    budget_micros: number;
    settled_micros: number;
    reserved_micros: number;
    next_estimate_micros: number;
    currency: 'USD';
  }
  | {
    id: string;
    kind: 'turn_limit';
    turns_used: number;
    max_turns: number;
  }
  | {
    id: string;
    kind: 'unknown_cost';
    operation: string;
  }
  | {
    id: string;
    kind: 'skills';
    message: string;
  };

type AskResearchPauseUpdate = { budget_usd: string } | { max_turns: number | null };

interface AskResearchPauseProps {
  pause: AskResearchPendingPause;
  onContinue(update?: AskResearchPauseUpdate): void;
  onSkip?: () => void;
  onStop(): void;
  disabled?: boolean;
}

const MONEY_PATTERN = /^\d+(?:\.\d{0,6})?$/;

function amount(value: string) {
  return `$${value}`;
}

function micros(value: number) {
  const compact = (value / 1_000_000).toFixed(6).replace(/\.?0+$/, '');
  const [whole, fraction = ''] = compact.split('.');
  return fraction.length >= 2 ? compact : `${whole}.${fraction.padEnd(2, '0')}`;
}

function Actions({
  onContinue,
  onSkip,
  onStop,
  disabled,
  continueDisabled = false,
}: {
  onContinue(): void;
  onSkip?: () => void;
  onStop(): void;
  disabled: boolean;
  continueDisabled?: boolean;
}) {
  return <div className="ask-research-pause-actions">
    <Button type="button" className="btn-primary" disabled={disabled || continueDisabled} onClick={onContinue}>Continue</Button>
    {onSkip && <Button type="button" disabled={disabled} onClick={onSkip}>Skip</Button>}
    <Button type="button" disabled={disabled} onClick={onStop}>Stop</Button>
  </div>;
}

function BudgetReached({ pause, onContinue, onStop, disabled = false }: Omit<AskResearchPauseProps, 'pause' | 'onSkip'> & {
  pause: Extract<AskResearchPendingPause, { kind: 'budget' }>;
}) {
  const currentBudget = micros(pause.budget_micros);
  const [budget, setBudget] = useState(currentBudget);
  const valid = MONEY_PATTERN.test(budget) && Number(budget) > Number(currentBudget);
  return <>
    <h3>Research budget reached</h3>
    <p>Spent {amount(micros(pause.settled_micros))} · reserved {amount(micros(pause.reserved_micros))} · next action about {amount(micros(pause.next_estimate_micros))}</p>
    <FormField label="New total budget (USD)" className="ask-research-pause-field">

      <TextInput type="text" inputMode="decimal" aria-label="New total budget (USD)" value={budget} disabled={disabled} onChange={(event) => setBudget(event.target.value.trim())} />
    </FormField>
    <Actions disabled={disabled} continueDisabled={!valid} onContinue={() => onContinue({ budget_usd: budget })} onStop={onStop} />
  </>;
}

function TurnLimitReached({ pause, onContinue, onStop, disabled = false }: Omit<AskResearchPauseProps, 'pause' | 'onSkip'> & {
  pause: Extract<AskResearchPendingPause, { kind: 'turn_limit' }>;
}) {
  const [turns, setTurns] = useState(String(pause.max_turns));
  const [noLimit, setNoLimit] = useState(false);
  const parsed = /^\d+$/.test(turns) ? Number(turns) : null;
  const valid = noLimit || parsed !== null && parsed > pause.max_turns;
  return <>
    <h3>Research turn limit reached</h3>
    <p>This run used {pause.turns_used} turns. Raise the limit or remove it to continue.</p>
    <FormField label="New max turns" className="ask-research-pause-field">

      <TextInput type="number" min={pause.max_turns + 1} step={1} aria-label="New max turns" value={turns} disabled={disabled || noLimit} onChange={(event) => setTurns(event.target.value)} />
    </FormField>
    <label className="ask-research-no-limit"><input type="checkbox" checked={noLimit} disabled={disabled} onChange={(event) => setNoLimit(event.target.checked)} />No limit</label>
    <Actions disabled={disabled} continueDisabled={!valid} onContinue={() => onContinue({ max_turns: noLimit ? null : parsed })} onStop={onStop} />
  </>;
}

const PENDING_LABELS: Record<AskResearchPendingPause['kind'], string> = {
  action: 'Consent to the next research action',
  budget: 'Research budget reached',
  turn_limit: 'Research turn limit reached',
  unknown_cost: 'Research cost unavailable',
  skills: 'Research skill unavailable',
};

export function AskResearchPause({ pause, onContinue, onSkip, onStop, disabled = false }: AskResearchPauseProps) {
  return <section className="ask-research-pause" aria-label={PENDING_LABELS[pause.kind]}>
    {pause.kind === 'action' && <>
      <h3>{pause.title}</h3>
      <p>{pause.effect}</p>
      {pause.targets.length > 0 && <p className="ask-research-targets">{pause.targets.join(' · ')}</p>}
      {pause.estimate_usd && <p className="ask-research-estimate">Estimated cost {amount(pause.estimate_usd)}</p>}
      <Actions
        disabled={disabled}
        onContinue={() => onContinue(undefined)}
        onSkip={pause.can_skip ? onSkip : undefined}
        onStop={onStop}
      />
    </>}
    {pause.kind === 'budget' && <BudgetReached key={JSON.stringify(pause)} pause={pause} onContinue={onContinue} onStop={onStop} disabled={disabled} />}
    {pause.kind === 'turn_limit' && <TurnLimitReached key={JSON.stringify(pause)} pause={pause} onContinue={onContinue} onStop={onStop} disabled={disabled} />}
    {pause.kind === 'unknown_cost' && <>
      <h3>Cost unavailable</h3>
      <p>This run cannot continue because the cost of its next {pause.operation} is unavailable. Stop it and choose a priced model or engine.</p>
      <div className="ask-research-pause-actions"><Button type="button" disabled={disabled} onClick={onStop}>Stop</Button></div>
    </>}
    {pause.kind === 'skills' && <>
      <h3>Research skill unavailable</h3>
      <p>{pause.message}</p>
      <Actions disabled={disabled} onContinue={() => onContinue(undefined)} onStop={onStop} />
    </>}
  </section>;
}
