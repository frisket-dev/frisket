import type { SourceRun } from '../../api/open';
import { formatUsd } from '../../format';

export function statusLabel(value: string | null | undefined): string {
  if (!value || value === 'never') return 'never run';
  return value.replaceAll('_', ' ');
}

export function formatWhen(value: string | null | undefined): string {
  if (!value) return 'never';
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export function formatDuration(ms: number | null): string {
  if (ms == null) return '';
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatCost(micro: number): string {
  return formatUsd(micro / 1_000_000);
}

export function runDeltaText(run: Pick<SourceRun, 'newRows' | 'changedRows' | 'skippedRows' | 'revisions'>): string {
  const parts: string[] = [];
  if (run.newRows) parts.push(`+${run.newRows} new`);
  if (run.changedRows) parts.push(`${run.changedRows} changed`);
  if (run.revisions) parts.push(`${run.revisions} revised`);
  if (run.skippedRows) parts.push(`${run.skippedRows} skipped`);
  return parts.join(' / ') || '+0 new';
}
