/** Language-pair controls for Opus-MT. Execution prepares missing models. */
import { useMemo } from 'react';
import type { DownloadablePair } from '../api/types';
import { PanelSelect } from './PanelSelect';

export interface TranslatePairPickerProps {
  /** Pair ids present in the current catalog. */
  installedPairs: string[];
  /** Downloadable pair roster from the pinned manifest. */
  downloadablePairs: DownloadablePair[];
  /** Selected source / target ISO codes (written back to the spec). */
  source: string;
  target: string;
  onSourceChange: (code: string) => void;
  onTargetChange: (code: string) => void;

}

interface Parsed {
  src: string;
  tgt: string;
}

// A single BCP-47-ish language subtag: a 2-3 letter primary, optionally one
// extension subtag (script/region), e.g. `en`, `zh`, `zh-hans`, `pt-br`.
const SIDE_RE = /^[a-z]{2,3}(-[a-z0-9]{1,8})?$/;

/**
 * Split a pair id into (src, tgt). A side may itself contain a `-`
 * (`zh-hans-en`), so — mirroring the backend grammar — try every split point
 * and accept the FIRST where both sides are valid language subtags. Returns
 * null for a genuinely unparseable pair (the old `split('-').length
 * === 2` dropped every multi-subtag pair).
 */
function parsePair(pair: string): Parsed | null {
  const parts = pair.split('-');
  for (let i = 1; i < parts.length; i++) {
    const src = parts.slice(0, i).join('-');
    const tgt = parts.slice(i).join('-');
    if (SIDE_RE.test(src) && SIDE_RE.test(tgt)) return { src, tgt };
  }
  return null;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
  if (bytes >= 1_000_000) return `${Math.round(bytes / 1_000_000)} MB`;
  if (bytes >= 1000) return `${Math.round(bytes / 1000)} KB`;
  return `${bytes} B`;
}

export function TranslatePairPicker({
  installedPairs,
  downloadablePairs,
  source,
  target,
  onSourceChange,
  onTargetChange,
}: TranslatePairPickerProps) {
  // Union of installed + downloadable pairs, each parsed into src/tgt.
  const allPairs = useMemo(() => {
    const set = new Set<string>([
      ...installedPairs,
      ...downloadablePairs.map((d) => d.pair),
    ]);
    return [...set].map(parsePair).filter((p): p is Parsed => p !== null);
  }, [installedPairs, downloadablePairs]);

  // Code -> English name, derived from each downloadable pair's display_name
  // ("English → Spanish"); falls back to the uppercased code.
  const langNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const d of downloadablePairs) {
      const parsed = parsePair(d.pair);
      if (!parsed) continue;
      const bits = d.display_name.split('→').map((s) => s.trim());
      if (bits.length === 2) {
        names[parsed.src] = bits[0];
        names[parsed.tgt] = bits[1];
      }
    }
    return names;
  }, [downloadablePairs]);

  const nameFor = (code: string) => langNames[code] ?? code.toUpperCase();

  const sourceOptions = useMemo(
    () => [...new Set(allPairs.map((p) => p.src))].sort(),
    [allPairs],
  );
  const targetOptions = useMemo(
    () =>
      [...new Set(allPairs.filter((p) => p.src === source).map((p) => p.tgt))].sort(),
    [allPairs, source],
  );

  const selectedPair = source && target ? `${source}-${target}` : '';
  const pairExists = allPairs.some((p) => p.src === source && p.tgt === target);
  const isInstalled = installedPairs.includes(selectedPair);
  const downloadable = downloadablePairs.find((d) => d.pair === selectedPair);
  const reversedExists = allPairs.some((p) => p.src === target && p.tgt === source);

  const swap = () => {
    if (!source || !target) return;
    onSourceChange(target);
    onTargetChange(source);
  };

  return (
    <div className="translate-pair-picker" data-testid="translate-pair-picker">
      <div className="translate-pair-row">
        <label className="form-label" htmlFor="translate-pair-source">
          Translate from
        </label>
        <PanelSelect
          id="translate-pair-source"
          testId="translate-pair-source"
          value={source}
          onValueChange={onSourceChange}
          options={[
            { value: '', label: 'Select…' },
            ...sourceOptions.map((code) => ({ value: code, label: nameFor(code) })),
          ]}
        />
        <button
          type="button"
          className="translate-pair-swap"
          data-testid="translate-pair-swap"
          disabled={!source || !target || !reversedExists}
          title="Swap languages"
          onClick={swap}
        >
          ⇄
        </button>
        <label className="form-label" htmlFor="translate-pair-target">
          to
        </label>
        <PanelSelect
          id="translate-pair-target"
          testId="translate-pair-target"
          value={target}
          onValueChange={onTargetChange}
          options={[
            { value: '', label: 'Select…' },
            ...targetOptions.map((code) => ({ value: code, label: nameFor(code) })),
          ]}
        />
      </div>

      {selectedPair && (
        <div className="translate-pair-status" data-testid="translate-pair-badge">
          {isInstalled ? (
            <span className="badge badge-installed" data-testid="translate-pair-installed">
              ✓ Installed
            </span>
          ) : downloadable ? (
            <span className="badge badge-downloadable">
              ↓ {formatBytes(downloadable.size)}
            </span>
          ) : pairExists ? (
            <span className="badge">Not installed</span>
          ) : (
            <span className="badge badge-unavailable" data-testid="translate-pair-unavailable">
              This pair is not available
            </span>
          )}
        </div>
      )}

      {pairExists && (
        <p className="form-hint">Downloads the required language model on first use.</p>
      )}
    </div>
  );
}
