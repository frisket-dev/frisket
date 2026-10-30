import { useRef, useState } from 'react';
import { GitCompareArrows } from 'lucide-react';
import { AnchoredMenu } from '../components/MenuPop';
import type { ActionRunAnchor } from '../grid/actionRunEntry';

export interface CompareRunChoice {
  anchor: ActionRunAnchor; label: string; summary?: string; group?: 'rows' | 'media';
  expected?: { sheetName: string; rowCount: number };
}
export interface CompareActionChoice { kind: string; label: string; group: 'rows' | 'media'; disabledReason?: string }
export interface CompareMenuProps {
  media?: boolean; inline?: boolean; onDismiss?(): void; runs: CompareRunChoice[]; actions: CompareActionChoice[];
  onCompareRun(anchor: ActionRunAnchor, expected: boolean): void;
  onCompareAction(kind: string): void; onJoinExpected(): void;
}

/** The workspace supplies catalog-backed, eligibility-checked choices and real run anchors. */
export function CompareMenu({ media = false, inline = false, onDismiss, runs, actions, onCompareRun, onCompareAction, onJoinExpected }: CompareMenuProps) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const choices = actions.filter((item) => media ? item.group === 'media' : true);
  const visibleRuns = runs.filter((item) => media ? item.group === 'media' : item.group !== 'media');
  const content = <>
      {visibleRuns.length > 0 && <><div className="panel-select-group">IN THIS SHEET</div>{visibleRuns.map((run) => <div key={`${run.anchor.runId}:${run.anchor.columnId}`}>
        <button type="button" role="menuitem" className="menu-item" onClick={() => { setOpen(false); onDismiss?.(); onCompareRun(run.anchor, false); }}><span className="menu-item-copy"><span className="menu-item-label">{run.label}</span><span className="menu-item-description">Try other models or instructions{run.summary ? ` · ${run.summary}` : ''}</span></span></button>
        {!media && run.expected && <button type="button" role="menuitem" className="menu-item" onClick={() => { setOpen(false); onDismiss?.(); onCompareRun(run.anchor, true); }}><span className="menu-item-copy"><span className="menu-item-label">{run.label} vs expected</span><span className="menu-item-description">{run.expected.sheetName} · {run.expected.rowCount} rows</span></span></button>}
      </div>)}</>}
      {(['rows', 'media'] as const).map((group) => {
        const items = choices.filter((choice) => choice.group === group);
        return items.length ? <div key={group}><div className="panel-select-group">{group === 'rows' ? 'TRY ON ROWS' : 'TRY ON DOCUMENTS & MEDIA'}</div>{items.map((item) => <button key={item.kind} type="button" role="menuitem" className="menu-item" disabled={!!item.disabledReason} title={item.disabledReason} onClick={() => { setOpen(false); onDismiss?.(); onCompareAction(item.kind); }}><span className="menu-item-copy"><span className="menu-item-label">{item.label}</span>{item.disabledReason && <span className="menu-item-description">{item.disabledReason}</span>}</span></button>)}</div> : null;
      })}
      {!media && <><div className="panel-select-group">EXPECTED ANSWERS</div><button type="button" role="menuitem" className="menu-item" onClick={() => { setOpen(false); onDismiss?.(); onJoinExpected(); }}>Check an action against answers…</button></>}
  </>;
  if (inline) return content;
  return <>
    <button ref={anchor} type="button" className="act-ribbon-primary" aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(!open)} data-testid="compare-menu-trigger">
      <span className="act-ribbon-primary-tile"><GitCompareArrows size={16} /></span><span className="act-ribbon-primary-label">Compare ⌄</span>
    </button>
    {open && <AnchoredMenu anchorRef={anchor} onClose={() => setOpen(false)} role="menu" width={320}>{content}</AnchoredMenu>}
  </>;
}
