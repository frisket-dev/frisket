import { useState } from 'react';
import './AskResearch.css';

export type AskResearchPendingApproval =
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

export type AskResearchApprovalUpdate = { budget_usd: string } | { max_turns: number | null };

export interface AskResearchApprovalProps {
  approval: AskResearchPendingApproval;
  onContinue(update?: AskResearchApprovalUpdate): void;
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
  return <div className="ask-research-approval-actions">
    <button type="button" className="btn btn-primary" disabled={disabled || continueDisabled} onClick={onContinue}>Continue</button>
    {onSkip && <button type="button" className="btn" disabled={disabled} onClick={onSkip}>Skip</button>}
    <button type="button" className="btn" disabled={disabled} onClick={onStop}>Stop</button>
  </div>;
}

function BudgetApproval({ approval, onContinue, onStop, disabled = false }: Omit<AskResearchApprovalProps, 'approval' | 'onSkip'> & {
  approval: Extract<AskResearchPendingApproval, { kind: 'budget' }>;
}) {
  const currentBudget = micros(approval.budget_micros);
  const [budget, setBudget] = useState(currentBudget);
  const valid = MONEY_PATTERN.test(budget) && Number(budget) > Number(currentBudget);
  return <>
    <h3>Research budget reached</h3>
    <p>Spent {amount(micros(approval.settled_micros))} · reserved {amount(micros(approval.reserved_micros))} · next action about {amount(micros(approval.next_estimate_micros))}</p>
    <label className="ask-research-approval-field">
      <span>New total budget (USD)</span>
      <input className="form-input" type="text" inputMode="decimal" aria-label="New total budget (USD)" value={budget} disabled={disabled} onChange={(event) => setBudget(event.target.value.trim())} />
    </label>
    <Actions disabled={disabled} continueDisabled={!valid} onContinue={() => onContinue({ budget_usd: budget })} onStop={onStop} />
  </>;
}

function TurnLimitApproval({ approval, onContinue, onStop, disabled = false }: Omit<AskResearchApprovalProps, 'approval' | 'onSkip'> & {
  approval: Extract<AskResearchPendingApproval, { kind: 'turn_limit' }>;
}) {
  const [turns, setTurns] = useState(String(approval.max_turns));
  const [noLimit, setNoLimit] = useState(false);
  const parsed = /^\d+$/.test(turns) ? Number(turns) : null;
  const valid = noLimit || parsed !== null && parsed > approval.max_turns;
  return <>
    <h3>Research turn limit reached</h3>
    <p>This run used {approval.turns_used} turns. Raise the limit or remove it to continue.</p>
    <label className="ask-research-approval-field">
      <span>New max turns</span>
      <input className="form-input" type="number" min={approval.max_turns + 1} step={1} aria-label="New max turns" value={turns} disabled={disabled || noLimit} onChange={(event) => setTurns(event.target.value)} />
    </label>
    <label className="ask-research-no-limit"><input type="checkbox" checked={noLimit} disabled={disabled} onChange={(event) => setNoLimit(event.target.checked)} />No limit</label>
    <Actions disabled={disabled} continueDisabled={!valid} onContinue={() => onContinue({ max_turns: noLimit ? null : parsed })} onStop={onStop} />
  </>;
}

export function AskResearchApproval({ approval, onContinue, onSkip, onStop, disabled = false }: AskResearchApprovalProps) {
  return <section className="ask-research-approval" aria-label="Research approval">
    {approval.kind === 'action' && <>
      <h3>{approval.title}</h3>
      <p>{approval.effect}</p>
      {approval.targets.length > 0 && <p className="ask-research-targets">{approval.targets.join(' · ')}</p>}
      {approval.estimate_usd && <p className="ask-research-estimate">Estimated cost {amount(approval.estimate_usd)}</p>}
      <Actions
        disabled={disabled}
        onContinue={() => onContinue(undefined)}
        onSkip={approval.can_skip ? onSkip : undefined}
        onStop={onStop}
      />
    </>}
    {approval.kind === 'budget' && <BudgetApproval key={JSON.stringify(approval)} approval={approval} onContinue={onContinue} onStop={onStop} disabled={disabled} />}
    {approval.kind === 'turn_limit' && <TurnLimitApproval key={JSON.stringify(approval)} approval={approval} onContinue={onContinue} onStop={onStop} disabled={disabled} />}
    {approval.kind === 'unknown_cost' && <>
      <h3>Cost unavailable</h3>
      <p>This run cannot continue because the cost of its next {approval.operation} is unavailable. Stop it and choose a priced model or engine.</p>
      <div className="ask-research-approval-actions"><button type="button" className="btn" disabled={disabled} onClick={onStop}>Stop</button></div>
    </>}
    {approval.kind === 'skills' && <>
      <h3>Research skill unavailable</h3>
      <p>{approval.message}</p>
      <Actions disabled={disabled} onContinue={() => onContinue(undefined)} onStop={onStop} />
    </>}
  </section>;
}
