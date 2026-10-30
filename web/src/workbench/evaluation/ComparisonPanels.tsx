import { useMemo, useState } from 'react';
import { GridCellKind } from '@glideapps/glide-data-grid';
import type { EvaluationResponse, EvaluationRule, SavedSummary } from '../../api/evaluation';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { TextInput } from '../../components/FormControls';
import { GridViewport } from '../../grid/GridViewport';
import { useGridTheme } from '../../grid/useGridTheme';
import { MATCH_RULES, scoreLabel } from './comparisonEvaluation';
import { matchAnswer } from '../evaluationMatching';
import { evaluationTheme } from './evaluationTheme';

export function ComparisonResults({ response, onChoose, onClose }: {
  response: EvaluationResponse; onChoose(field: string, variant: string): void; onClose(): void;
}) {
  const { gridTheme } = useGridTheme();
  const evaluationColors = useMemo(() => evaluationTheme(gridTheme), [gridTheme]);
  const columns = [{ id: 'variant', title: 'Variant', width: 190 },
    ...response.output_columns.filter((column) => response.link?.fields.some((field) => field.logical_key === column.logical_key))
      .map((column) => ({ id: column.logical_key, title: column.current_name, width: 190 }))];
  return <Modal title="Results" size="wide" onClose={onClose} closeLabel="Close results" testId="comparison-results">
    <div className="comparison-results-grid" style={{ height: Math.min(440, 72 + response.variants.length * 38) }}>
      <GridViewport columns={columns} rows={response.variants.length} rowHeight={38} theme={gridTheme}
        getCellContent={([column, row]) => {
          const variant = response.variants[row];
          const score = variant?.scores[columns[column]?.id];
          const text = column === 0 ? `${variant?.key} · ${variant?.label}` : `${scoreLabel(score?.accuracy)}${score?.remaining ? ` · ${score.scored} scored · ${score.remaining} left` : ''}${score && score.coverage < 1 ? ` · ${score.attempted}/${score.checked} run` : ''}`;
          return { kind: GridCellKind.Text, data: text, displayData: text, readonly: true, allowOverlay: false };
        }}
        drawCell={(args, draw) => {
          draw();
          const score = response.variants[args.row]?.scores[columns[args.col]?.id];
          if (args.col === 0 || score?.accuracy == null) return;
          const best = response.best[columns[args.col].id]?.includes(response.variants[args.row].key);
          args.ctx.save(); args.ctx.fillStyle = best ? evaluationColors.match : args.theme.textMedium;
          args.ctx.fillRect(args.rect.x + 8, args.rect.y + args.rect.height - 4, (args.rect.width - 16) * score.accuracy, 2); args.ctx.restore();
        }}
        onCellActivated={([column, row]) => { if (column > 0) onChoose(columns[column].id, response.variants[row].key); }} />
    </div>
  </Modal>;
}

export function MatchRuleDialog({ response, field, busy, onRule, onEditAll, onClose }: {
  response: EvaluationResponse; field: string; busy: boolean;
  onRule(rule: EvaluationRule): void; onEditAll(): void; onClose(): void;
}) {
  const current = response.link?.fields.find((item) => item.logical_key === field)?.rule ?? 'exact';
  return <Modal title="Match rule" onClose={onClose} closeLabel="Close match rule" testId="comparison-match-rule" busy={busy}>
    <div className="comparison-rule-options" role="radiogroup" aria-label="Match rule">
      {MATCH_RULES.map(({ value, label }) => <label key={value} className="comparison-rule-option">
        <input type="radio" name="comparison-rule" value={value} checked={current === value} disabled={busy} onChange={() => onRule(value)} />
        <span><strong>{label}</strong><small>{response.variants.map((variant) => {
          const before = variant.scores[field]; const after = response.rule_previews[field]?.[value]?.[variant.key];
          const stale = new Set((variant as typeof variant & { stale_source_row_ids?: number[] }).stale_source_row_ids ?? []);
          const eligibleRows = response.row_ids.filter((rowId) => !stale.has(rowId));
          const canCount = value !== 'hand' && eligibleRows.every((rowId) => !variant.rows[String(rowId)]?.[field]?.bounded_value && !response.expected_bounded?.[String(rowId)]?.[field]);
          const flipped = canCount ? eligibleRows.filter((rowId) => {
            const cell = variant.rows[String(rowId)]?.[field];
            return matchAnswer(value, response.expected[String(rowId)]?.[field] ?? null, cell?.value ?? null, { state: cell?.state ?? 'missing' }) !== variant.verdicts[String(rowId)]?.[field];
          }).length : null;
          return `${variant.key} ${scoreLabel(before?.accuracy)} → ${scoreLabel(after?.accuracy)}${flipped ? ` · ${flipped} ${flipped === 1 ? 'row flips' : 'rows flip'}` : ''}`;
        }).join(' · ')}</small></span>
      </label>)}
      <Button disabled={busy} onClick={onEditAll}>Edit all fields…</Button>
    </div>
  </Modal>;
}

export function ManageShelf({ items, busy, onRename, onDelete, onClose }: {
  items: SavedSummary[]; busy: boolean; onRename(id: number, label: string): void;
  onDelete(id: number): void; onClose(): void;
}) {
  return <Modal title="Manage shelf" onClose={onClose} closeLabel="Close shelf" busy={busy} testId="comparison-manage-shelf">
    <div className="comparison-shelf-list">{items.length ? items.map((item) => <ShelfRow key={item.id} item={item} busy={busy} onRename={onRename} onDelete={onDelete} />)
      : <p>No shelved results.</p>}</div>
  </Modal>;
}

function ShelfRow({ item, busy, onRename, onDelete }: {
  item: SavedSummary; busy: boolean; onRename(id: number, label: string): void; onDelete(id: number): void;
}) {
  const [label, setLabel] = useState(item.label);
  return <div className="comparison-shelf-item">
    <TextInput aria-label={`Name for ${item.label}`} value={label} disabled={busy} onChange={(event) => setLabel(event.target.value)} />
    <small>{item.row_count} rows · {new Date(item.saved_at).toLocaleDateString()}</small>
    <Button disabled={busy || !label.trim() || label === item.label} onClick={() => onRename(item.id, label.trim())}>Rename</Button>
    <Button disabled={busy} onClick={() => onDelete(item.id)}>Delete</Button>
  </div>;
}
