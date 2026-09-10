import { formatTimecode } from '../../temporal/model';
import type {
  ParsedPointDraft,
  ParsedRangeDraft,
} from '../../temporal/draft';
import './temporal-actions.css';

export function PointDraftPreview({
  items,
  durationMs,
  purpose = 'media',
}: {
  items: ParsedPointDraft[];
  durationMs?: number;
  purpose?: 'media' | 'transcript';
}) {
  const uniqueInternal = new Set(items.filter((item) => (
    item.at_ms > 0 && (durationMs == null || item.at_ms < durationMs)
  )).map((item) => item.at_ms));
  const duplicates = items.length - new Set(items.map((item) => item.at_ms)).size;
  const startEdgeCount = items.filter((item) => item.at_ms === 0).length;
  const endEdgeCount = durationMs == null
    ? 0
    : items.filter((item) => item.at_ms === durationMs).length;
  return (
    <div className="temporal-preview" data-testid="split-draft-preview">
      {uniqueInternal.size > 0 ? (
        <>
          <strong>{uniqueInternal.size + 1} segments</strong>
          <span className="muted"> after sorting the timestamps; repeated times count once</span>
        </>
      ) : (
        <strong>No internal split timestamps</strong>
      )}
      <ol>
        {items.map((item) => (
          <li key={item.id}>{formatTimecode(item.at_ms)}{item.label ? ` · ${item.label}` : ''}</li>
        ))}
      </ol>
      {duplicates > 0 && (
        <p className="temporal-warning" data-testid="split-duplicate-warning">
          {duplicates} repeated {duplicates === 1 ? 'timestamp uses' : 'timestamps use'} the same split time.
        </p>
      )}
      {startEdgeCount > 0 && (
        <p className="temporal-warning" data-testid="split-edge-warning">
          0:00 is the start of the source, so it will not create an extra segment.
        </p>
      )}
      {endEdgeCount > 0 && (
        <p className="temporal-warning" data-testid="split-end-edge-warning">
          A timestamp at the end of the source will not create an extra segment.
        </p>
      )}
      <p className="form-hint">
        {purpose === 'transcript'
          ? 'A timestamp inside a transcript chunk includes that whole chunk in both neighboring rows.'
          : 'Frisket checks the source length and includes the final segment when you run this.'}
      </p>
    </div>
  );
}

export function RangeDraftPreview({
  items,
  purpose = 'media',
}: {
  items: ParsedRangeDraft[];
  purpose?: 'media' | 'transcript';
}) {
  const coordinateKeys = items.map((item) => `${item.start_ms}:${item.end_ms}`);
  const duplicates = coordinateKeys.length - new Set(coordinateKeys).size;
  let overlapPairs = 0;
  for (let left = 0; left < items.length; left += 1) {
    for (let right = left + 1; right < items.length; right += 1) {
      const a = items[left];
      const b = items[right];
      const sameCoordinates = a.start_ms === b.start_ms && a.end_ms === b.end_ms;
      if (!sameCoordinates && a.start_ms < b.end_ms && b.start_ms < a.end_ms) {
        overlapPairs += 1;
      }
    }
  }
  return (
    <div className="temporal-preview" data-testid="split-draft-preview">
      <strong>{items.length} selected {items.length === 1 ? 'range' : 'ranges'}</strong>
      <ol>
        {items.map((item) => (
          <li key={item.id}>
            {formatTimecode(item.start_ms)} – {formatTimecode(item.end_ms)}
            {item.label ? ` · ${item.label}` : ''}
          </li>
        ))}
      </ol>
      {duplicates > 0 && (
        <p className="temporal-warning" data-testid="split-range-duplicate-warning">
          {duplicates} repeated {duplicates === 1
            ? 'range will create an additional copy of the same clip.'
            : 'ranges will create additional copies of the same clips.'}
        </p>
      )}
      {overlapPairs > 0 && (
        <p className="temporal-warning" data-testid="split-range-overlap-warning">
          {overlapPairs} overlapping range {overlapPairs === 1 ? 'pair means' : 'pairs mean'} some moments will appear in more than one clip.
        </p>
      )}
      <p className="form-hint">
        {purpose === 'transcript'
          ? 'Transcript rows are created in this order. Gaps are skipped, and overlapping or repeated ranges stay separate. Boundaries include any transcript chunk they touch.'
          : 'Clips are created in this order. Gaps are skipped, and overlapping or repeated ranges stay separate. Frisket checks that every range fits the source.'}
      </p>
    </div>
  );
}
