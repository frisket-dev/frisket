// @vitest-environment jsdom
//
// TranslatePairPicker is the Google-Translate-style source ⇄ swap ⇄ target
// control for the local Opus-MT translate engine, with
// per-pair installed/downloadable badges and an INLINE one-click download that
// reuses ModelPullProgress in place (never ejects the user to another page).

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    startArtifactPull: vi.fn(),
    getModelPull: vi.fn(),
    cancelModelPull: vi.fn(),
  };
});

import { TranslatePairPicker } from '../../src/components/TranslatePairPicker';
import { startArtifactPull } from '../../src/api/open';
import type { DownloadablePair } from '../../src/api/types';

const DOWNLOADABLE: DownloadablePair[] = [
  { pair: 'en-es', display_name: 'English → Spanish', size: 298_000_000, license: 'CC-BY-4.0' },
  { pair: 'es-en', display_name: 'Spanish → English', size: 297_000_000, license: 'CC-BY-4.0' },
  { pair: 'de-en', display_name: 'German → English', size: 300_000_000, license: 'CC-BY-4.0' },
];

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPicker(overrides: Partial<React.ComponentProps<typeof TranslatePairPicker>> = {}) {
  const onSourceChange = vi.fn();
  const onTargetChange = vi.fn();
  render(
    <TranslatePairPicker
      installedPairs={overrides.installedPairs ?? []}
      downloadablePairs={overrides.downloadablePairs ?? DOWNLOADABLE}
      source={overrides.source ?? ''}
      target={overrides.target ?? ''}
      onSourceChange={overrides.onSourceChange ?? onSourceChange}
      onTargetChange={overrides.onTargetChange ?? onTargetChange}
    />,
  );
  return { onSourceChange, onTargetChange };
}

describe('TranslatePairPicker', () => {
  it('shows an installed badge for an already-installed pair', () => {
    renderPicker({ installedPairs: ['en-es'], source: 'en', target: 'es' });
    expect(screen.getByTestId('translate-pair-installed')).toBeInTheDocument();
    // no inline download for an installed pair
    expect(screen.queryByTestId('translate-pair-download')).toBeNull();
  });

  it('shows a downloadable size badge + inline download for an uninstalled pair', () => {
    renderPicker({ source: 'en', target: 'es' });
    expect(screen.getByTestId('translate-pair-badge')).toHaveTextContent('298 MB');
    expect(screen.getByTestId('translate-pair-download')).toBeInTheDocument();
  });

  // badge-installed/
  // badge-downloadable/badge-unavailable had zero CSS rules and fell back to
  // the bare .badge alert-red pill (built for StatusBar's unread-count
  // chip) — installed/downloadable/unavailable were visually identical.
  // Pin the three distinct modifier classes actually land on the DOM (the
  // CSS itself — styles.css — gives each its own quiet tone).
  it('applies distinct badge modifier classes per pair state', () => {
    renderPicker({ installedPairs: ['en-es'], source: 'en', target: 'es' });
    expect(screen.getByTestId('translate-pair-installed')).toHaveClass('badge', 'badge-installed');
  });

  it('applies the downloadable badge modifier class', () => {
    renderPicker({ source: 'en', target: 'es' });
    expect(screen.getByTestId('translate-pair-badge').querySelector('.badge')).toHaveClass(
      'badge-downloadable',
    );
  });

  it('applies the unavailable badge modifier class for a nonexistent pair', () => {
    // 'fr' isn't a source in any downloadable/installed pair, so 'fr'->'es'
    // doesn't exist at all (distinct from "exists but not installed").
    renderPicker({ downloadablePairs: DOWNLOADABLE, source: 'de', target: 'es' });
    // 'de' only pairs with 'en' in the fixture roster, so de-es doesn't exist.
    expect(screen.getByTestId('translate-pair-unavailable')).toHaveClass(
      'badge',
      'badge-unavailable',
    );
  });

  it('writes the source code back on change', () => {
    const onSourceChange = vi.fn();
    renderPicker({ onSourceChange });
    fireEvent.change(screen.getByTestId('translate-pair-source'), {
      target: { value: 'en' },
    });
    expect(onSourceChange).toHaveBeenCalledWith('en');
  });

  it('writes the target code back on change (target options depend on source)', () => {
    const onTargetChange = vi.fn();
    // source is fixed to 'en' so 'es' is a valid target option.
    renderPicker({ source: 'en', onTargetChange });
    fireEvent.change(screen.getByTestId('translate-pair-target'), {
      target: { value: 'es' },
    });
    expect(onTargetChange).toHaveBeenCalledWith('es');
  });

  // "Translate from"/"to"
  // used to be two bare native <select className="form-input">, a third
  // "pick a language" idiom alongside PanelSelect (the rest of ActionForm,
  // including the sibling EngineLanguageControl this picker swaps places
  // with) and TranslateCompareTab's row-height-select. Both now route
  // through PanelSelect — its own contract (PanelSelect.tsx) keeps the real
  // <select> as the interaction trigger (so the `fireEvent.change`/
  // selectOption tests above keep working unmodified) while opening a
  // popover on pointer interaction; that popover's presence is what
  // distinguishes it from a bare select.
  it('opens a PanelSelect popover for the source picker instead of a bare native dropdown', async () => {
    const user = userEvent.setup();
    renderPicker({ source: 'en' });
    const sourceSelect = screen.getByTestId('translate-pair-source');
    expect(screen.queryByTestId('translate-pair-source-menu')).not.toBeInTheDocument();
    await user.click(sourceSelect);
    expect(screen.getByTestId('translate-pair-source-menu')).toBeInTheDocument();
  });

  it('swap exchanges source and target when the reverse pair exists', () => {
    const onSourceChange = vi.fn();
    const onTargetChange = vi.fn();
    renderPicker({ source: 'en', target: 'es', onSourceChange, onTargetChange });
    fireEvent.click(screen.getByTestId('translate-pair-swap'));
    expect(onSourceChange).toHaveBeenCalledWith('es');
    expect(onTargetChange).toHaveBeenCalledWith('en');
  });

  it('inline download starts a pull and renders progress in place', async () => {
    (startArtifactPull as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      pull: {
        id: 7,
        model: 'opus-mt:en-es',
        status: 'running',
        phase: 'downloading',
        total_bytes: null,
        completed_bytes: null,
        error: null,
        resolved_digest: null,
        resolved_size: null,
        created_at: '2026-07-17T00:00:00Z',
        started_at: '2026-07-17T00:00:01Z',
        finished_at: null,
        cancel_requested: false,
      },
      deduplicated: false,
    });
    renderPicker({ source: 'en', target: 'es' });
    await act(async () => {
      fireEvent.click(screen.getByTestId('translate-pair-download'));
    });
    expect(startArtifactPull).toHaveBeenCalledWith('opus-mt:en-es');
    // ModelPullProgress mounts in place (its root testid).
    expect(screen.getByTestId('model-pull-progress')).toBeInTheDocument();
  });
});


// --- completion, switch-mid-pull, disabled controls, retry ---

import { getModelPull } from '../../src/api/open';
import type { ModelPullDto } from '../../src/api/types';



function runningPull(overrides: Partial<ModelPullDto> = {}): ModelPullDto {
  return {
    id: 7,
    model: 'opus-mt:en-es',
    status: 'running',
    phase: 'downloading',
    total_bytes: null,
    completed_bytes: null,
    error: null,
    resolved_digest: null,
    resolved_size: null,
    created_at: '2026-07-17T00:00:00Z',
    started_at: '2026-07-17T00:00:01Z',
    finished_at: null,
    cancel_requested: false,
    artifact: { kind: 'ct2_pair', source_url: 's', license: 'CC-BY-4.0', manifest_version: 'v' },
    ...overrides,
  };
}

describe('TranslatePairPicker inline pull lifecycle', () => {
  it('freezes the language controls while a pull is streaming', async () => {
    (startArtifactPull as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      pull: runningPull(),
      deduplicated: false,
    });
    renderPicker({ source: 'en', target: 'es' });
    await act(async () => {
      fireEvent.click(screen.getByTestId('translate-pair-download'));
    });
    expect(screen.getByTestId('translate-pair-source')).toBeDisabled();
    expect(screen.getByTestId('translate-pair-target')).toBeDisabled();
    expect(screen.getByTestId('translate-pair-swap')).toBeDisabled();
  });

  it('calls onInstalled with the pull-bound pair even after switching', async () => {
    vi.useFakeTimers();
    try {
      (startArtifactPull as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
        pull: runningPull(),
        deduplicated: false,
      });
      (getModelPull as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
        runningPull({ status: 'done', phase: 'done' }),
      );
      const onInstalled = vi.fn();
      const onSourceChange = vi.fn();
      const onTargetChange = vi.fn();
      // start a pull for en-es
      const { rerender } = render(
        <TranslatePairPicker
          installedPairs={[]}
          downloadablePairs={DOWNLOADABLE}
          source="en"
          target="es"
          onSourceChange={onSourceChange}
          onTargetChange={onTargetChange}
          onInstalled={onInstalled}
        />,
      );
      await act(async () => {
        fireEvent.click(screen.getByTestId('translate-pair-download'));
      });
      // parent switches the selection mid-pull (controls are disabled in the UI,
      // but a prop change could still arrive) — the bound pair must win.
      rerender(
        <TranslatePairPicker
          installedPairs={[]}
          downloadablePairs={DOWNLOADABLE}
          source="de"
          target="en"
          onSourceChange={onSourceChange}
          onTargetChange={onTargetChange}
          onInstalled={onInstalled}
        />,
      );
      // advance the poll -> observes 'done'
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100);
      });
      expect(onInstalled).toHaveBeenCalledWith('en-es');
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears to a retryable state with an error on a failed pull', async () => {
    vi.useFakeTimers();
    try {
      (startArtifactPull as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
        pull: runningPull(),
        deduplicated: false,
      });
      (getModelPull as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
        runningPull({ status: 'failed', error: { code: 'checksum_mismatch', message: 'bad checksum' } }),
      );
      renderPicker({ source: 'en', target: 'es' });
      await act(async () => {
        fireEvent.click(screen.getByTestId('translate-pair-download'));
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100);
      });
      // download button returns (retryable) and the error is visible
      expect(screen.getByTestId('translate-pair-download')).toBeInTheDocument();
      expect(screen.getByTestId('translate-pair-download-error')).toHaveTextContent('bad checksum');
    } finally {
      vi.useRealTimers();
    }
  });
});
