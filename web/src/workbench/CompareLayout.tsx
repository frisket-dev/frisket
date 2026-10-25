import type { HTMLAttributes, ReactNode } from 'react';
import { Loader2, Settings2, X } from 'lucide-react';
import type { EngineOption } from '../api/open';
import { tierForEngine } from '../actions/engineCatalog';
import { EngineTierBadge } from '../components/EngineTierBadge';
import { IconButton } from '../components/IconButton';
import type { PipState } from './mediaCompareSession';
import './MediaCompareShell.css';

const PIP_GLYPH: Record<Exclude<PipState, 'running'>, string> = {
  idle: '', pending: '◷', results: '✓', error: '!',
};

const PIP_STATUS: Record<PipState, string> = {
  idle: 'Not run', pending: 'Pending', running: 'Running', results: 'Results ready', error: 'Failed',
};

/** Engine selection precedes the run controls in reading and narrow-screen order. */
export function CompareToolbar({
  testIdPrefix, running, controls, children, note, disabled,
  label = 'Variants', tag = 'Temporary comparison',
  chipsTestId = `${testIdPrefix}-variant-chips`,
}: {
  testIdPrefix: string;
  running: boolean;
  controls: ReactNode;
  children: ReactNode;
  note?: ReactNode;
  /** A supplied value preserves Transcribe's native disabled fieldset. */
  disabled?: boolean;
  label?: string | null;
  tag?: string | null;
  chipsTestId?: string;
}) {
  const engines = <>
    {tag && <span className="ocr-compare-scratch-tag" data-testid={`${testIdPrefix}-scratch-tag`}>{tag}</span>}
    {label && <span className="ocr-compare-variants-label">{label}</span>}
    <div className="ocr-compare-variant-chips" data-testid={chipsTestId}>{children}</div>
  </>;
  const row = { className: 'ocr-compare-chips-row', 'data-testid': `${testIdPrefix}-chips-row` };
  return <div className="ocr-compare-toolbar" data-testid={`${testIdPrefix}-toolbar`} data-running={running ? 'true' : 'false'}>
    {disabled === undefined ? <div {...row}>{engines}</div> : <fieldset {...row} disabled={disabled}>{engines}</fieldset>}
    <div className="ocr-compare-controls-row" data-testid={`${testIdPrefix}-controls-row`}>{controls}</div>
    {note}
  </div>;
}

/** One engine chip, optionally exposing the media variant's configure controls. */
export function CompareEngineChip({
  testIdPrefix, engine, engineId, label, variantId, pip, summary,
  onConfigure, onBodyClick = onConfigure, onRemove, disabled,
  chipTestId = `${testIdPrefix}-${onConfigure ? 'variant' : 'engine'}-chip`,
  removeTestId = onConfigure ? `${testIdPrefix}-variant-remove` : `${testIdPrefix}-engine-chip-remove-${engineId ?? ''}`,
}: {
  testIdPrefix: string;
  engine?: EngineOption;
  engineId: string | null;
  label: string;
  variantId?: string;
  pip?: PipState;
  summary?: string;
  onConfigure?(): void;
  onBodyClick?(): void;
  onRemove(): void;
  disabled?: boolean;
  chipTestId?: string;
  removeTestId?: string;
}) {
  const name = <>
    <span className="ocr-compare-variant-name">{label}</span>
    {engine && <EngineTierBadge tier={tierForEngine(engine)} testId={`${testIdPrefix}-${onConfigure ? 'variant-tier' : 'engine-chip-tier'}`} />}
    {summary && <span className="ocr-compare-variant-summary muted"> · {summary}</span>}
  </>;
  return <div className="ocr-compare-variant-chip engine-tier-chip" data-testid={chipTestId}
    data-engine-id={engineId ?? ''} data-variant-id={variantId} data-pip={pip}>
    {pip && <span className="ocr-compare-pip" data-pip={pip}>
      <span aria-hidden>{pip === 'running'
        ? <Loader2 size={10} className="ocr-compare-spin" />
        : PIP_GLYPH[pip]}</span>
      <span className="sr-only">Status: {PIP_STATUS[pip]}</span>
    </span>}
    {onConfigure ? <>
      <button type="button" className="ocr-compare-variant-body" data-testid={`${testIdPrefix}-variant-body`}
        disabled={disabled} onClick={onBodyClick} title="Configure variant">{name}</button>
      <IconButton icon={Settings2} size="inline" className="ocr-compare-variant-gear"
        data-testid={`${testIdPrefix}-variant-gear`} label={`Configure ${label}`}
        disabled={disabled} onClick={onConfigure} />
    </> : name}
    <IconButton icon={X} size="inline" className="ocr-compare-variant-remove"
      data-testid={removeTestId} label={`Remove ${label}`} disabled={disabled} onClick={onRemove} />
  </div>;
}

/** Common result geometry; each comparison retains its domain result renderer. */
export function CompareResultColumn({ header, details, children, className, textClassName, ...props }: HTMLAttributes<HTMLDivElement> & {
  header: ReactNode;
  details?: ReactNode;
  textClassName?: string;
}) {
  return <div {...props} className={['ocr-compare-engine-column', className].filter(Boolean).join(' ')}>
    <div className="ocr-compare-engine-head">{header}</div>
    {details}
    <div className={['ocr-compare-engine-text', textClassName].filter(Boolean).join(' ')}>{children}</div>
  </div>;
}
