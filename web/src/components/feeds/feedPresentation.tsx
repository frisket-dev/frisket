import { Globe2, ListVideo, Rss, Scale } from 'lucide-react';
import type { FeedKind } from '../../api/open';
import { StatusChip, type StatusTone } from '../PanelPrimitives';
import { statusLabel } from './feedFormatting';

export function FeedKindIcon({ kind }: { kind: FeedKind }) {
  switch (kind) {
    case 'youtube_playlist':
    case 'youtube_channel':
      return <ListVideo size={12} aria-hidden />;
    case 'api_list_dicts':
      return <Globe2 size={12} aria-hidden />;
    case 'courtlistener_docket':
      return <Scale size={12} aria-hidden />;
    default:
      return <Rss size={12} aria-hidden />;
  }
}

export function FeedStatus({ value, testId }: { value: string | null | undefined; testId?: string }) {
  const tones: Record<string, StatusTone> = {
    ok: 'success', healthy: 'success', error: 'error', failing: 'error', stale: 'warning',
  };
  return <StatusChip tone={tones[value ?? ''] ?? 'neutral'} size="sm" uppercase testId={testId}>
    {statusLabel(value)}
  </StatusChip>;
}
