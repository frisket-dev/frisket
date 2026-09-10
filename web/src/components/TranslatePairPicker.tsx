/**
 * Google-Translate-style paired source ⇄ swap ⇄ target control for the local
 * Opus-MT translate engine. For a pair engine the PAIR is the availability unit,
 * so this replaces the generic source picker + target text input when
 * `engine === 'opus_mt'`.
 *
 * Each selectable pair carries a badge — installed ✓ or downloadable + size.
 * When the selected pair is not installed, the form shows an INLINE one-click
 * download that reuses the existing ModelPullProgress streaming surface in
 * place (never ejecting the user to another page — the "manage models" link is
 * the secondary path). On completion the parent is told which pair installed so
 * it flips to installed and the run unblocks; the parent also refetches the
 * catalog so a remount reflects the new install.
 */
import { useMemo, useState } from 'react';

import {
  ApiError,
  type DownloadablePair,
  type ModelPullDto,
  startArtifactPull,
} from '../api/open';
import { ModelPullProgress } from './ModelPullProgress';
import { PanelSelect } from './PanelSelect';

export interface TranslatePairPickerProps {
  /** Installed pair ids ("en-es") — the parent-owned merged set (engine.models
   *  plus any inline-installed-this-session). */
  installedPairs: string[];
  /** Downloadable pair roster from the pinned manifest. */
  downloadablePairs: DownloadablePair[];
  /** Selected source / target ISO codes (written back to the spec). */
  source: string;
  target: string;
  onSourceChange: (code: string) => void;
  onTargetChange: (code: string) => void;
  /** Called with the pair id after an inline install completes, so the parent
   *  can mark it installed (run-gating) and refetch the catalog. */
  onInstalled?: (pair: string) => void;
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

// A pull bound to the exact pair that initiated it — the pair is
// immutable in the pull state, never re-read from the (possibly since-switched)
// selection at completion time.
interface BoundPull {
  pair: string;
  dto: ModelPullDto;
}

export function TranslatePairPicker({
  installedPairs,
  downloadablePairs,
  source,
  target,
  onSourceChange,
  onTargetChange,
  onInstalled,
}: TranslatePairPickerProps) {
  const [pull, setPull] = useState<BoundPull | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

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

  // While a pull is streaming, freeze the language controls — the
  // pull is bound to a specific pair and switching mid-stream is confusing.
  const pulling = pull !== null;

  const swap = () => {
    if (!source || !target || pulling) return;
    onSourceChange(target);
    onTargetChange(source);
  };

  const download = async () => {
    if (!selectedPair) return;
    const pair = selectedPair;
    setStarting(true);
    setStartError(null);
    try {
      const result = await startArtifactPull(`opus-mt:${pair}`);
      setPull({ pair, dto: result.pull });
    } catch (err) {
      // Joining an in-progress pull (409 pull_busy) is not an error — render
      // the already-active pull's progress, exactly like LocalServerGuidance.
      if (err instanceof ApiError && err.details && typeof err.details === 'object') {
        const active = (err.details as { active?: ModelPullDto }).active;
        if (active && typeof active.id === 'number') {
          setPull({ pair, dto: active });
          setStarting(false);
          return;
        }
      }
      setStartError(err instanceof Error ? err.message : 'Download failed.');
    } finally {
      setStarting(false);
    }
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
          disabled={pulling}
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
          disabled={!source || !target || !reversedExists || pulling}
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
          disabled={pulling}
          onValueChange={onTargetChange}
          options={[
            { value: '', label: 'Select…' },
            ...targetOptions.map((code) => ({ value: code, label: nameFor(code) })),
          ]}
        />
      </div>

      {selectedPair && !pulling && (
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

      {/* Inline install — reuse ModelPullProgress in place; do NOT eject. */}
      {!isInstalled && downloadable && !pulling && (
        <div className="translate-pair-install">
          <button
            type="button"
            className="button"
            data-testid="translate-pair-download"
            disabled={starting}
            onClick={() => void download()}
          >
            {starting ? 'Starting…' : `Download ${downloadable.display_name}`}
          </button>
          {startError && (
            <p className="form-error" data-testid="translate-pair-download-error">
              {startError}
            </p>
          )}
        </div>
      )}

      {pull && (
        <ModelPullProgress
          pull={pull.dto}
          onDone={() => {
            // Bind to the pull's OWN pair, not the current
            // selection (which the user may have switched away from).
            onInstalled?.(pull.pair);
            setPull(null);
          }}
          onFailed={(dto) => {
            // A failed/cancelled pull clears back to a retryable state with a
            // visible error — the download button returns.
            setPull(null);
            setStartError(
              dto.error?.message ??
                (dto.status === 'cancelled'
                  ? 'Download cancelled.'
                  : 'Download failed. Try again.'),
            );
          }}
        />
      )}
    </div>
  );
}
