// @vitest-environment jsdom
//
// One engine dropdown replaces the old two-level tier-chips + PanelSelect combo
// (ActionForm.tsx's retired `engineTierPicker`). Braintrust-style searchable
// menu grouped by tier, same popover mechanism as ModelPicker.tsx (see that
// component's own test file for the mechanism coverage this reuses via the
// shared popover polyfill rather than re-testing).

import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { act, cleanup, screen, waitFor, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { EnginePicker } from '../../src/components/EnginePicker';
import type { EngineOption } from '../../src/api/types';
import { installPopoverPolyfill } from '../support/domPolyfills';



beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(cleanup);

// Real production data (actions/transcribeEngineCatalog.ts's
// TRANSCRIBE_ENGINE_FALLBACK): sibling ids that differ ONLY by '_' vs '-'
// (local `faster_whisper` vs sidecar `faster-whisper`) — the regression case
// for EnginePicker's testid-slugging.
function engines(): EngineOption[] {
  return [
    { id: 'faster_whisper', label: 'Whisper (local, default)', tier: 'local', available: true },
    { id: 'parakeet', label: 'Parakeet (local, ONNX)', tier: 'local', available: true },
    {
      id: 'faster-whisper',
      label: 'Whisper quality tier (sidecar)',
      tier: 'sidecar',
      available: true,
      models: ['small'],
    },
    {
      id: 'remote',
      label: 'Remote provider speech API',
      tier: 'hosted',
      billable: true,
      available: false,
      error: 'Action catalog unavailable',
    },
  ];
}

function renderPicker(value = 'faster_whisper') {
  const onChange = vi.fn();
  render(
    <>
      <span id="engine-label">Engine</span>
      <EnginePicker engines={engines()} value={value} onChange={onChange} ariaLabelledBy="engine-label" />
    </>,
  );
  return { onChange };
}

describe('EnginePicker', () => {
  it('exposes only the visible picker trigger and menu options', async () => {
    renderPicker('faster_whisper');
    expect(screen.queryByTestId('engine-select')).not.toBeInTheDocument();
    const trigger = screen.getByTestId('engine-picker-button');
    expect(trigger).toBeVisible();
    expect(trigger).toHaveAccessibleName(/engine.*whisper \(local, default\)/i);

    await userEvent.click(trigger);
    await userEvent.type(screen.getByTestId('engine-picker-search'), 'remote');
    expect(screen.getByTestId('engine-option-remote')).toHaveAttribute('aria-disabled', 'true');
  });

  it('distinguishes sibling ids that differ only by _ vs - (no data-testid collision)', async () => {
    renderPicker('faster_whisper');
    await userEvent.click(screen.getByTestId('engine-picker-button'));
    // Search mode flattens across tiers, so both `faster_whisper` (local) and
    // `faster-whisper` (sidecar) are on screen together — a lossy slug (both
    // collapsing to the same string) would make one of these ambiguous or
    // simply overwrite the other's selector/testid.
    await userEvent.type(screen.getByTestId('engine-picker-search'), 'whisper');
    expect(screen.getByTestId('engine-option-faster_whisper')).toBeInTheDocument();
    expect(screen.getByTestId('engine-option-faster-whisper')).toBeInTheDocument();
    expect(screen.getByTestId('engine-option-faster_whisper')).not.toBe(
      screen.getByTestId('engine-option-faster-whisper'),
    );
  });

  it('groups engines by tier, disables the unavailable one, and selecting an option fires onChange + closes', async () => {
    const { onChange } = renderPicker('faster_whisper');
    await userEvent.click(screen.getByTestId('engine-picker-button'));
    expect(screen.getByTestId('engine-picker-menu')).toBeInTheDocument();

    expect(screen.getByTestId('engine-picker-tier-local')).toHaveTextContent('Local');
    expect(screen.getByTestId('engine-picker-tier-sidecar')).toHaveTextContent('Sidecar');
    expect(screen.getByTestId('engine-picker-tier-hosted')).toHaveTextContent('Hosted');

    await userEvent.click(screen.getByTestId('engine-picker-tier-sidecar'));
    const sidecarOption = screen.getByTestId('engine-option-faster-whisper');
    expect(sidecarOption).toHaveTextContent('small'); // engineNoteLine's models summary
    await userEvent.click(sidecarOption);

    expect(onChange).toHaveBeenCalledWith('faster-whisper');
    expect(screen.queryByTestId('engine-picker-menu')).not.toBeInTheDocument();
  });

  it('an unavailable engine renders disabled and clicking it does not select it', async () => {
    const { onChange } = renderPicker('faster_whisper');
    await userEvent.click(screen.getByTestId('engine-picker-button'));
    await userEvent.click(screen.getByTestId('engine-picker-tier-hosted'));

    const remoteOption = screen.getByTestId('engine-option-remote');
    expect(remoteOption).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(remoteOption);
    expect(onChange).not.toHaveBeenCalled();
    // Stays open — an unavailable click is a no-op, not a dismiss.
    expect(screen.getByTestId('engine-picker-menu')).toBeInTheDocument();
  });

  it('search narrows across every tier by label/id/tier text', async () => {
    renderPicker('faster_whisper');
    await userEvent.click(screen.getByTestId('engine-picker-button'));
    await userEvent.type(screen.getByTestId('engine-picker-search'), 'parakeet');

    expect(screen.getByTestId('engine-option-parakeet')).toBeInTheDocument();
    expect(screen.queryByTestId('engine-option-faster_whisper')).not.toBeInTheDocument();
    expect(screen.queryByTestId('engine-option-remote')).not.toBeInTheDocument();
  });

  // No keyboard support existed at all (no
  // Arrow/Home/End navigation, no Enter-to-select, no Escape-close) —
  // ModelPicker.tsx lacks it too (noted there as a backport candidate), but
  // this picker gets the standard combobox/listbox behavior now.
  describe('keyboard navigation', () => {
    it('ArrowDown moves the highlighted option and Enter selects it', async () => {
      const { onChange } = renderPicker('faster_whisper');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      const search = screen.getByTestId('engine-picker-search');

      // Local tier is active by default (2 engines: faster_whisper,
      // parakeet) — one ArrowDown from the initially-highlighted selected
      // engine moves to the second.
      await userEvent.type(search, '{ArrowDown}');
      expect(screen.getByTestId('engine-option-parakeet')).toHaveClass('is-active');
      expect(search).toHaveAttribute('aria-activedescendant', 'engine-option-parakeet');

      await userEvent.type(search, '{Enter}');
      expect(onChange).toHaveBeenCalledWith('parakeet');
      expect(screen.queryByTestId('engine-picker-menu')).not.toBeInTheDocument();
    });

    it('ArrowUp moves the highlight back up', async () => {
      renderPicker('faster_whisper');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      const search = screen.getByTestId('engine-picker-search');

      await userEvent.type(search, '{ArrowDown}{ArrowDown}');
      expect(screen.getByTestId('engine-option-parakeet')).toHaveClass('is-active');
      await userEvent.type(search, '{ArrowUp}');
      expect(screen.getByTestId('engine-option-faster_whisper')).toHaveClass('is-active');
    });

    it('Home/End jump to the first/last option in the current tier', async () => {
      renderPicker('faster_whisper');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      const search = screen.getByTestId('engine-picker-search');

      await userEvent.type(search, '{End}');
      expect(screen.getByTestId('engine-option-parakeet')).toHaveClass('is-active');
      await userEvent.type(search, '{Home}');
      expect(screen.getByTestId('engine-option-faster_whisper')).toHaveClass('is-active');
    });

    it('Escape closes the menu and returns focus to the trigger button', async () => {
      renderPicker('faster_whisper');
      const button = screen.getByTestId('engine-picker-button');
      await userEvent.click(button);
      const search = screen.getByTestId('engine-picker-search');
      search.focus();

      await userEvent.type(search, '{Escape}');
      expect(screen.queryByTestId('engine-picker-menu')).not.toBeInTheDocument();

      // Focus-return runs on the next animation frame (same idiom as
      // ModelPicker's own "focus returns to the trigger on close" test).
      await act(async () => {
        await new Promise((resolve) => requestAnimationFrame(resolve));
      });
      expect(button).toHaveFocus();
    });

    it('Enter does not select a disabled (unavailable) engine', async () => {
      const { onChange } = renderPicker('faster_whisper');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      await userEvent.click(screen.getByTestId('engine-picker-tier-hosted'));
      const search = screen.getByTestId('engine-picker-search');

      await userEvent.type(search, '{ArrowDown}');
      expect(screen.getByTestId('engine-option-remote')).toHaveClass('is-active');
      await userEvent.type(search, '{Enter}');
      expect(onChange).not.toHaveBeenCalled();
      expect(screen.getByTestId('engine-picker-menu')).toBeInTheDocument();
    });

    it('focus leaving the portal (Tab away) closes the menu', async () => {
      render(
        <>
          <span id="engine-label">Engine</span>
          <EnginePicker engines={engines()} value="faster_whisper" onChange={vi.fn()} ariaLabelledBy="engine-label" />
          <button type="button" data-testid="outside-button">Outside</button>
        </>,
      );
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      expect(screen.getByTestId('engine-picker-menu')).toBeInTheDocument();

      screen.getByTestId('outside-button').focus();
      await waitFor(() => expect(screen.queryByTestId('engine-picker-menu')).not.toBeInTheDocument());
    });
  });

  // Engine-tier-visibility lane: every option row carries a small text badge
  // naming where the engine runs (local / sidecar / hosted) — the tier stays
  // visible even in flat search results where the tier panes are absent —
  // and an unavailable engine renders the catalog's own reason (policy or
  // config) visibly, never only a tooltip on a bare disabled row.
  describe('engine tier badges + unavailability reasons', () => {
    it('renders a tier badge on every option row, including flat search results', async () => {
      renderPicker('faster_whisper');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      await userEvent.type(screen.getByTestId('engine-picker-search'), 'whisper');

      const localBadge = screen.getByTestId('engine-option-tier-faster_whisper');
      expect(localBadge).toHaveTextContent('local');
      expect(localBadge).toHaveAttribute('data-tier', 'local');
      const sidecarBadge = screen.getByTestId('engine-option-tier-faster-whisper');
      expect(sidecarBadge).toHaveTextContent('sidecar');
      expect(sidecarBadge).toHaveAttribute('data-tier', 'sidecar');
    });

    it('badges a hosted engine as hosted in its tier pane', async () => {
      renderPicker('faster_whisper');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      await userEvent.click(screen.getByTestId('engine-picker-tier-hosted'));

      const hostedBadge = screen.getByTestId('engine-option-tier-remote');
      expect(hostedBadge).toHaveTextContent('hosted');
      expect(hostedBadge).toHaveAttribute('data-tier', 'hosted');
    });

    it('renders the catalog reason on an unavailable row and no reason on available ones', async () => {
      renderPicker('faster_whisper');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      await userEvent.click(screen.getByTestId('engine-picker-tier-hosted'));

      expect(screen.getByTestId('engine-option-reason-remote')).toHaveTextContent(
        'Action catalog unavailable',
      );

      await userEvent.click(screen.getByTestId('engine-picker-tier-local'));
      expect(screen.queryByTestId('engine-option-reason-faster_whisper')).not.toBeInTheDocument();
    });
  });

  // Only a FLAGGED (restricted/gated/
  // custom) engine carries a `license` field at all — permissive engines
  // never render the hint, not even an empty/neutral one.
  describe('engine license hints', () => {
    function licensedEngines(): EngineOption[] {
      return [
        { id: 'markitdown', label: 'MarkItDown (local, default)', tier: 'local', available: true },
        {
          id: 'chandra',
          label: 'Chandra 2 document VLM (sidecar, experimental)',
          tier: 'sidecar',
          available: true,
          license: {
            name: 'Modified OpenRAIL-M (Datalab)',
            url: 'https://github.com/datalab-to/chandra/blob/master/MODEL_LICENSE',
            note: 'Free under $2M revenue/funding; use must not compete with Datalab products.',
            restricted: true,
          },
        },
      ];
    }

    function renderLicensedPicker(value: string) {
      const onChange = vi.fn();
      render(
        <>
          <span id="engine-label">Engine</span>
          <EnginePicker engines={licensedEngines()} value={value} onChange={onChange} ariaLabelledBy="engine-label" />
        </>,
      );
      return { onChange };
    }

    it('renders no license hint when the selected engine is unflagged', () => {
      renderLicensedPicker('markitdown');
      expect(screen.queryByTestId('engine-license-hint')).not.toBeInTheDocument();
    });

    it('renders one uniform hint sentence (engine label + restricted) when a flagged engine is selected', () => {
      renderLicensedPicker('chandra');
      const hint = screen.getByTestId('engine-license-hint');
      // The sentence is derived from the engine's own label + the restricted
      // flag — NOT from per-engine copy — so it names the engine, not the
      // license, and carries no license detail itself.
      expect(hint).toHaveTextContent('Chandra 2 document VLM (sidecar, experimental) has a restrictive license');
      expect(hint).not.toHaveTextContent('Modified OpenRAIL-M');
      expect(screen.queryByTestId('engine-license-popover')).not.toBeInTheDocument();
    });

    it('clicking "details" opens a popover with the license name, note, and a working external link', async () => {
      renderLicensedPicker('chandra');
      expect(screen.queryByTestId('engine-license-popover')).not.toBeInTheDocument();

      await userEvent.click(screen.getByTestId('engine-license-hint-details'));

      const popover = screen.getByTestId('engine-license-popover');
      expect(popover).toHaveTextContent('Modified OpenRAIL-M (Datalab)');
      expect(popover).toHaveTextContent(
        'Free under $2M revenue/funding; use must not compete with Datalab products.',
      );
      const link = screen.getByTestId('engine-license-popover-link');
      expect(link).toHaveAttribute(
        'href',
        'https://github.com/datalab-to/chandra/blob/master/MODEL_LICENSE',
      );
      expect(link).toHaveAttribute('target', '_blank');
      expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    });

    it('clicking "details" again closes the popover', async () => {
      renderLicensedPicker('chandra');
      const detailsButton = screen.getByTestId('engine-license-hint-details');
      await userEvent.click(detailsButton);
      expect(screen.getByTestId('engine-license-popover')).toBeInTheDocument();

      await userEvent.click(detailsButton);
      expect(screen.queryByTestId('engine-license-popover')).not.toBeInTheDocument();
    });

    // The "View license" link was portaled to <body> and never received focus
    // on open, so it was
    // unreachable by keyboard — Tab moved past the trigger to the next real
    // page control and the global focusin closer dismissed the popover before
    // the link was ever reachable. This is the keyboard-only regression test
    // for that fix: open with Enter (not a click), assert focus actually
    // landed IN the popover, Tab does not escape it, and Escape restores
    // focus to the trigger.
    it('keyboard-only: Enter opens the popover with focus on the link, Tab cannot escape it, Escape restores focus to the trigger', async () => {
      renderLicensedPicker('chandra');
      const detailsButton = screen.getByTestId('engine-license-hint-details');

      detailsButton.focus();
      expect(detailsButton).toHaveFocus();
      await userEvent.keyboard('{Enter}');

      const link = await screen.findByTestId('engine-license-popover-link');
      await waitFor(() => expect(link).toHaveFocus());

      // Tab (and Shift+Tab) must not carry focus out of the popover — with a
      // single focusable descendant the trap cycles back onto the link
      // itself rather than releasing focus to whatever real page control
      // sits after the (DOM-distant, portaled-to-<body>) trigger.
      await userEvent.keyboard('{Tab}');
      expect(link).toHaveFocus();
      expect(screen.getByTestId('engine-license-popover')).toBeInTheDocument();

      await userEvent.keyboard('{Shift>}{Tab}{/Shift}');
      expect(link).toHaveFocus();
      expect(screen.getByTestId('engine-license-popover')).toBeInTheDocument();

      await userEvent.keyboard('{Escape}');
      expect(screen.queryByTestId('engine-license-popover')).not.toBeInTheDocument();
      await waitFor(() => expect(detailsButton).toHaveFocus());
    });

    it('switching selection away from a flagged engine removes the hint', async () => {
      // A controlled wrapper (unlike renderLicensedPicker's fire-and-forget
      // onChange mock) — this test needs the trigger's own displayed
      // selection to actually move, not just onChange to be called.
      function ControlledPicker() {
        const [value, setValue] = useState('chandra');
        return (
          <>
            <span id="engine-label">Engine</span>
            <EnginePicker engines={licensedEngines()} value={value} onChange={setValue} ariaLabelledBy="engine-label" />
          </>
        );
      }
      render(<ControlledPicker />);
      expect(screen.getByTestId('engine-license-hint')).toBeInTheDocument();

      await userEvent.click(screen.getByTestId('engine-picker-button'));
      await userEvent.type(screen.getByTestId('engine-picker-search'), 'markitdown');
      await userEvent.click(screen.getByTestId('engine-option-markitdown'));
      expect(screen.queryByTestId('engine-license-hint')).not.toBeInTheDocument();
    });

    it('marks a flagged row with a license badge in the open menu, pre-selection', async () => {
      renderLicensedPicker('markitdown');
      await userEvent.click(screen.getByTestId('engine-picker-button'));
      await userEvent.click(screen.getByTestId('engine-picker-tier-sidecar'));

      expect(screen.getByTestId('engine-license-badge-chandra')).toBeInTheDocument();
      // The unflagged, currently-active tier's row carries no badge at all.
      expect(screen.queryByTestId('engine-license-badge-markitdown')).not.toBeInTheDocument();
    });
  });
});
