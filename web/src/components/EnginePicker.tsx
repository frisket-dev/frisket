// ONE engine-selection dropdown per action. Braintrust-style searchable menu
// grouped by tier (local/sidecar/hosted) instead of provider, mirroring
// ModelPicker.tsx's own provider-grouped pattern exactly (same portal/popover/
// anchoring plumbing) so the two pickers read as one family. Unavailable engines
// stay listed (disabled, tagged with their tier) rather than disappearing — the
// engine-availability disclosure (kept in ActionForm, rendered below this
// component) is where the per-engine error detail lives; this picker only needs
// to say "can't pick this one, here's its tier".
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { ChevronRight, Lock } from 'lucide-react';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import type { EngineOption } from '../api/types';
import {
  engineModelsSummary,
  engineTierLabel,
  engineTierOptions,
  engineUnavailableReason,
  tierForEngine,
} from '../actions/engineCatalog';
import { EngineTierBadge } from './EngineTierBadge';
import type { EngineTier } from '../api/types';

// Unlike ModelPicker's optionSlug, this keeps '_' distinct from '-': real
// engine catalogs carry sibling ids that differ only by that separator (e.g.
// transcribeEngineCatalog.ts's local `faster_whisper` vs sidecar
// `faster-whisper`) — collapsing both to '-' would give them the same
// data-testid and make them untargetable individually.
const optionSlug = (value: string): string =>
  value.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');

/** Secondary line under an engine's name: pricing (when the catalog carries
 *  it), else nothing; models list rides alongside when present. Mirrors
 *  ModelPicker's modelNote split, but EngineOption has no dash-delimited
 *  label convention to split — the data is already structured
 *  (pricing/models), so this composes from fields instead of parsing text. */
function engineNoteLine(engine: EngineOption): string {
  const bits: string[] = [];
  if (engine.description) bits.push(engine.description);
  const pricing = engine.pricing;
  if (pricing) {
    if (!pricing.billable || pricing.unit_price_usd === 0) {
      bits.push('free');
    } else if (pricing.unit_price_usd_string) {
      bits.push(pricing.unit_price_usd_string);
    } else if (pricing.description) {
      bits.push(pricing.description);
    }
  }
  const models = engineModelsSummary(engine);
  if (models) bits.push(models);
  return bits.join(' · ');
}

function tierInitial(tier: EngineTier): string {
  return engineTierLabel(tier).slice(0, 1).toUpperCase();
}

export interface EnginePickerOrdinaryChoice {
  id: string;
  label: string;
  note?: string;
  available?: boolean;
  unavailableReason?: string | null;
}

interface EnginePickerProps {
  engines: EngineOption[];
  /** Choices such as Auto or an unknown saved identity that have no truthful
   * execution tier. They render outside the tier groups. */
  ordinaryChoices?: EnginePickerOrdinaryChoice[];
  value: string;
  onChange: (engineId: string) => void;
  /** id of an external label element — same aria-labelledby contract as
   *  ModelPicker (the trigger announces "<label> <selected engine>"). */
  ariaLabelledBy: string;
}

export function EnginePicker({
  engines,
  ordinaryChoices = [],
  value,
  onChange,
  ariaLabelledBy,
}: EnginePickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeTier, setActiveTier] = useState<EngineTier | null>(null);
  // The keyboard-highlighted option, tracked
  // separately from `value` (the actually-selected engine) — same
  // aria-activedescendant idiom as TargetSaveToControl's OutputNameCombobox
  // (that file's own comment has the full rationale: one highlight that
  // moves via both arrow keys and mouse hover, without ever moving real DOM
  // focus off the search input).
  const [activeOptionId, setActiveOptionId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  // Engine license hints (design revision — the initial
  // full-detail inline line read too long, cut down to one short generic
  // sentence + a "details" popover): a SECOND small anchored popover,
  // independent of the main tier/search menu above.
  const licenseButtonRef = useRef<HTMLButtonElement | null>(null);
  const licensePopoverRef = useRef<HTMLDivElement | null>(null);
  const licenseLinkRef = useRef<HTMLAnchorElement | null>(null);
  const [licensePopoverOpen, setLicensePopoverOpen] = useState(false);

  const tiers = useMemo(() => engineTierOptions(engines), [engines]);

  const menuPos = useAnchoredPosition(buttonRef, {
    enabled: open,
    align: 'right',
    width: (_rect, viewportWidth) => Math.min(480, viewportWidth - 16),
    gap: 4,
    minHeight: 220,
  });

  // Reuses the SAME anchored-position primitive as the main menu (above) and
  // ModelPicker — small fixed width, no tier/search panes, just a name + note
  // + link.
  const licensePopoverPos = useAnchoredPosition(licenseButtonRef, {
    enabled: licensePopoverOpen,
    align: 'left',
    width: 280,
    gap: 4,
    minHeight: 60,
  });

  // Native Popover API lifecycle — identical recipe to ModelPicker.tsx (see
  // that file's comment for the full rationale: manual mode for no Escape
  // light-dismiss, top-layer paint order, deferred search focus).
  useLayoutEffect(() => {
    const el = menuRef.current;
    if (!open || !el) return undefined;
    if (el.getAttribute('popover') !== 'manual') el.setAttribute('popover', 'manual');
    if (el.isConnected && !el.matches(':popover-open')) el.showPopover();
    const raf = requestAnimationFrame(() => searchRef.current?.focus());
    return () => {
      cancelAnimationFrame(raf);
      if (el.isConnected && el.matches(':popover-open')) el.hidePopover();
    };
  }, [open]);

  // Same manual-popover lifecycle as the main menu, scoped to the small
  // license-details popover.
  useLayoutEffect(() => {
    const el = licensePopoverRef.current;
    if (!licensePopoverOpen || !el) return undefined;
    if (el.getAttribute('popover') !== 'manual') el.setAttribute('popover', 'manual');
    if (el.isConnected && !el.matches(':popover-open')) el.showPopover();
    return () => {
      if (el.isConnected && el.matches(':popover-open')) el.hidePopover();
    };
  }, [licensePopoverOpen]);

  // The popover is portaled to
  // <body>, so its DOM position has no relation to the "details" trigger's
  // position in the page's Tab order — opening it left focus stranded on the
  // trigger, and the global focusin closer below then dismissed the popover
  // the instant Tab moved focus to whatever real page control came next,
  // before the "View license" link was ever reachable. Fix, same shape as
  // CopilotDialogPopover/AddColumnPopover's own trigger-capture +
  // focus-on-open + focus-restore-on-close idiom (workspace/popovers.tsx):
  // move focus INTO the popover the moment it opens.
  useEffect(() => {
    if (!licensePopoverOpen) return undefined;
    const raf = requestAnimationFrame(() => licenseLinkRef.current?.focus());
    return () => cancelAnimationFrame(raf);
  }, [licensePopoverOpen]);

  // A mini-dialog (role="dialog" below) traps Tab within its own focusable
  // set while open, same expectation as a native <dialog> (OutputColumn
  // CollisionModal's showModal() gets this for free from the platform; this
  // is a portaled div, not a <dialog>, so it's done by hand). Today there is
  // exactly one focusable descendant (the license link) — cycling Tab/
  // Shift+Tab back onto itself at the boundary — but this reads the DOM live
  // so it stays correct if the popover ever grows a second control.
  useEffect(() => {
    if (!licensePopoverOpen) return undefined;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const container = licensePopoverRef.current;
      if (!container) return;
      const focusables = Array.from(
        container.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || !container.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !container.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [licensePopoverOpen]);

  // Every dismissal path (Escape, an outside click, focus programmatically
  // landing elsewhere) closes AND hands focus back to the "details" trigger
  // that opened it — the same restore-on-dismiss idiom as
  // CopilotDialogPopover/AddColumnPopover (workspace/popovers.tsx), so
  // keyboard users never lose their place.
  const dismissLicensePopover = useCallback(() => {
    setLicensePopoverOpen(false);
    requestAnimationFrame(() => licenseButtonRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!licensePopoverOpen) return undefined;
    // The Tab trap above keeps focus from LEAVING the popover while it's
    // open; this focusin listener is the backstop for focus landing outside
    // by some other means (a click target that isn't caught by the
    // pointerdown listener, or a programmatic .focus() elsewhere) — the
    // popover's own contents (and the trigger button) still read as
    // "inside" via the same `contains()` checks the pointerdown listener
    // uses below.
    const closeIfOutside = (event: PointerEvent | FocusEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (licenseButtonRef.current?.contains(target)) return;
      if (licensePopoverRef.current?.contains(target)) return;
      dismissLicensePopover();
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') dismissLicensePopover();
    };
    document.addEventListener('pointerdown', closeIfOutside, true);
    document.addEventListener('focusin', closeIfOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeIfOutside, true);
      document.removeEventListener('focusin', closeIfOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [licensePopoverOpen, dismissLicensePopover]);

  useEffect(() => {
    if (!open) return undefined;
    const closeIfOutside = (event: PointerEvent | FocusEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (rootRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('pointerdown', closeIfOutside, true);
    // Tab (or any other focus move) out of the
    // portaled menu didn't close it before — nothing dismissed a picker left
    // open by keyboard once the pointer stopped driving it. `focusin` fires
    // on the element gaining focus, so this is the mirror of the pointerdown
    // listener above: same rootRef/menuRef "inside" check, different event.
    document.addEventListener('focusin', closeIfOutside);
    return () => {
      document.removeEventListener('pointerdown', closeIfOutside, true);
      document.removeEventListener('focusin', closeIfOutside);
    };
  }, [open]);

  const searching = query.trim().length > 0;
  const searchMatches = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const engineMatches = engines.filter((engine) => {
      const haystack = `${engine.label} ${engine.id} ${engineTierLabel(tierForEngine(engine))}`.toLowerCase();
      return tokens.every((token) => haystack.includes(token));
    });
    const ordinaryMatches = ordinaryChoices.filter((choice) => {
      const haystack = `${choice.label} ${choice.id} ${choice.note ?? ''}`.toLowerCase();
      return tokens.every((token) => haystack.includes(token));
    });
    return [...ordinaryMatches, ...engineMatches];
  }, [engines, ordinaryChoices, query]);

  const resolvedActiveTier =
    (activeTier && tiers.some((t) => t.tier === activeTier) ? activeTier : null) ??
    (engines.find((e) => e.id === value) ? tierForEngine(engines.find((e) => e.id === value)!) : null) ??
    tiers[0]?.tier ??
    null;
  const activeTierEngines = tiers.find((t) => t.tier === resolvedActiveTier)?.engines ?? [];
  // The flat list arrow/Home/End/Enter navigate over: search results when
  // searching, else the untiered ordinary choices followed by the active
  // tier's engines — the same list the menu actually renders in either mode.
  const currentList = searching ? searchMatches : [...ordinaryChoices, ...activeTierEngines];
  const effectiveActiveOptionId = open
    ? (
      currentList.some((engine) => engine.id === activeOptionId)
        ? activeOptionId
        : currentList.find((engine) => engine.id === value)?.id ?? currentList[0]?.id ?? null
    )
    : null;

  // Keep the highlighted option in view as arrow keys move it past the
  // visible scroll window (same as OutputNameCombobox's own effect).
  useEffect(() => {
    if (!effectiveActiveOptionId) return;
    const el = document.getElementById(`engine-option-${optionSlug(effectiveActiveOptionId)}`);
    if (typeof el?.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' });
  }, [effectiveActiveOptionId]);

  const changeEngine = (engineId: string) => {
    // License details belong to the previously selected engine. Close them
    // as part of the selection event without restoring focus to that old
    // engine's details button.
    setLicensePopoverOpen(false);
    onChange(engineId);
  };

  const selectEngine = (engineId: string) => {
    changeEngine(engineId);
    const returnFocus = Boolean(menuRef.current?.contains(document.activeElement));
    setOpen(false);
    setQuery('');
    if (returnFocus) requestAnimationFrame(() => buttonRef.current?.focus());
  };

  const closeAndReturnFocus = () => {
    setOpen(false);
    setQuery('');
    requestAnimationFrame(() => buttonRef.current?.focus());
  };

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeAndReturnFocus();
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      const activeEngine = currentList.find((engine) => engine.id === effectiveActiveOptionId);
      if (activeEngine && activeEngine.available !== false) selectEngine(activeEngine.id);
      return;
    }
    if (
      event.key !== 'ArrowDown' && event.key !== 'ArrowUp' &&
      event.key !== 'Home' && event.key !== 'End'
    ) return;
    event.preventDefault();
    if (!currentList.length) return;
    const currentIndex = currentList.findIndex((engine) => engine.id === effectiveActiveOptionId);
    let nextIndex: number;
    if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = currentList.length - 1;
    else {
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      nextIndex = currentIndex < 0 ? 0 : Math.max(0, Math.min(currentList.length - 1, currentIndex + delta));
    }
    setActiveOptionId(currentList[nextIndex].id);
  };

  const renderOption = (engine: EngineOption | EnginePickerOrdinaryChoice) => {
    const unavailable = engine.available === false;
    const catalogEngine = engines.find((candidate) => candidate.id === engine.id);
    const ordinaryChoice = engine as EnginePickerOrdinaryChoice;
    const reason = catalogEngine ? engineUnavailableReason(catalogEngine)
      : ordinaryChoice.unavailableReason ?? null;
    const note = catalogEngine ? engineNoteLine(catalogEngine) : ordinaryChoice.note ?? '';
    const highlighted = engine.id === effectiveActiveOptionId;
    return (
      <button
        key={engine.id}
        type="button"
        id={`engine-option-${optionSlug(engine.id)}`}
        className={`model-option${engine.id === value ? ' is-selected' : ''}${unavailable ? ' is-unavailable' : ''}${highlighted ? ' is-active' : ''}`}
        data-testid={`engine-option-${optionSlug(engine.id)}`}
        role="option"
        aria-selected={engine.id === value}
        aria-disabled={unavailable}
        title={reason ?? undefined}
        tabIndex={-1}
        onMouseEnter={() => setActiveOptionId(engine.id)}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          if (unavailable) return;
          selectEngine(engine.id);
        }}
      >
        <span className="model-option-text">
          <span className="model-option-label">
            {engine.label}
            {/* The tier rides on every row (not
                only the tier pane), so flat search results stay honest about
                where each engine runs. */}
            {catalogEngine && <EngineTierBadge
              tier={tierForEngine(catalogEngine)}
              testId={`engine-option-tier-${optionSlug(engine.id)}`}
            />}
            {unavailable && <span className="engine-tier-unavailable"> · unavailable</span>}
            {/* Engine license hints: a quiet pre-selection marker
                on flagged rows in the open menu, so the restriction is
                visible before committing to this engine — the hint sentence
                + "details" popover render under the trigger once selected,
                below. */}
            {catalogEngine?.license && (
              <span
                className="engine-license-badge"
                data-testid={`engine-license-badge-${optionSlug(engine.id)}`}
                title={
                  catalogEngine.license.note
                    ? `${catalogEngine.license.name} — ${catalogEngine.license.note}`
                    : catalogEngine.license.name
                }
              >
                <Lock size={10} aria-hidden /> license
              </span>
            )}
          </span>
          {note && <span className="model-option-note">{note}</span>}
          {/* Unavailability honesty: the
              catalog's own reason — policy (network off) or config (no key /
              sidecar not configured) — renders visibly, never only a
              tooltip on a bare disabled row. */}
          {reason && (
            <span
              className="model-option-note engine-option-reason"
              data-testid={`engine-option-reason-${optionSlug(engine.id)}`}
            >
              {reason}
            </span>
          )}
        </span>
        {engine.id === value && <span className="model-option-check" aria-hidden>✓</span>}
      </button>
    );
  };

  const selectedEngine = engines.find((e) => e.id === value);
  const selectedOrdinary = ordinaryChoices.find((choice) => choice.id === value);
  const selectedUnavailable = selectedEngine?.available === false || selectedOrdinary?.available === false;
  const selectedUnavailableReason = selectedEngine
    ? engineUnavailableReason(selectedEngine)
    : selectedOrdinary?.unavailableReason ?? null;
  const selectedTier = selectedEngine ? tierForEngine(selectedEngine) : undefined;
  const selectedName = (selectedEngine?.label ?? selectedOrdinary?.label ?? value) || 'Choose engine…';
  const selectedSub = [
    selectedTier ? engineTierLabel(selectedTier) : '',
    selectedEngine ? engineNoteLine(selectedEngine) : selectedOrdinary?.note ?? '',
  ].filter(Boolean).join(' · ');
  const selectedValueId = `${ariaLabelledBy}-value`;

  return (
    <div className="model-picker engine-picker" ref={rootRef}>
      <button
        type="button"
        ref={buttonRef}
        className="form-input model-picker-button engine-picker-button"
        data-testid="engine-picker-button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={`${ariaLabelledBy} ${selectedValueId}`}
        onClick={() => {
          // Opening fresh: let effectiveActiveOptionId's own fallback pick
          // the current value's option (or the list's first) rather than
          // resuming wherever a previous session's highlight was left.
          setOpen((prev) => {
            const next = !prev;
            if (next) setActiveOptionId(null);
            return next;
          });
        }}
      >
        <span className="model-avatar" aria-hidden>
          {selectedTier ? tierInitial(selectedTier) : '·'}
        </span>
        <span className="model-picker-selected" id={selectedValueId}>
          <span className="model-picker-selected-name">
            {selectedName}
            {/* The open menu marks unavailable rows (renderOption above), but
                the CLOSED trigger showed the selected engine exactly like a
                working one — a draft, replay, or revoked provider key can
                leave an unrunnable engine selected, and the only hint was a
                disabled Run button further down. The badge rides the trigger
                so the state is legible without opening anything. */}
            {selectedUnavailable && (
              <span className="engine-tier-unavailable" data-testid="engine-picker-selected-unavailable">
                {' '}· unavailable
              </span>
            )}
          </span>
          {selectedSub && <span className="model-picker-selected-sub">{selectedSub}</span>}
        </span>
      </button>

      {selectedUnavailableReason && (
        <p className="engine-selected-unavailable-reason" data-testid="engine-picker-selected-reason">
          {/* Authored sentence, backend detail demoted to <code> — the same
              "this token is raw/technical, not prose" split the action panel's
              engine-availability disclosure uses. */}
          {selectedName} can’t run here — <code>{selectedUnavailableReason}</code>
        </p>
      )}

      {/* Engine license hints: rendered ONLY when the
          SELECTED engine carries a flagged (`restricted: true`) license —
          permissive engines carry no `license` field at all, so this whole
          block is absent for them, never a blank/neutral line. ONE uniform
          sentence for every restricted engine, derived from the engine's own
          label + the restricted flag (NOT per-engine copy) — the license
          name/note/url themselves are read straight from the catalog
          `license` object in the popover below, never duplicated into this
          sentence. Sits outside the trigger <button> (not nested inside it)
          so "details" stays a real, independently-clickable button. */}
      {selectedEngine?.license?.restricted && (
        <p className="engine-license-hint" data-testid="engine-license-hint">
          <Lock size={11} className="engine-license-hint-icon" aria-hidden />
          {selectedEngine.label} has a restrictive license —{' '}
          <button
            type="button"
            ref={licenseButtonRef}
            className="engine-license-hint-details"
            data-testid="engine-license-hint-details"
            aria-haspopup="dialog"
            aria-expanded={licensePopoverOpen}
            onClick={() => setLicensePopoverOpen((prev) => !prev)}
          >
            details
          </button>
        </p>
      )}

      {licensePopoverOpen &&
        selectedEngine?.license &&
        createPortal(
          // A mini-dialog: role="dialog" + aria-labelledby (pointing at its
          // own name line) rather than a bespoke aria-label string, the same
          // pairing DocumentView's document-options MenuPop and
          // OutputColumnCollisionModal's <dialog> both use for their own
          // titled popovers/modals.
          <div
            role="dialog"
            aria-labelledby={`${selectedValueId}-license-name`}
            className="engine-license-popover"
            data-testid="engine-license-popover"
            ref={licensePopoverRef}
            style={
              licensePopoverPos
                ? {
                    top: licensePopoverPos.top,
                    bottom: licensePopoverPos.bottom,
                    left: licensePopoverPos.left,
                    width: licensePopoverPos.width,
                  }
                : { visibility: 'hidden' }
            }
          >
            <div className="engine-license-popover-name" id={`${selectedValueId}-license-name`}>
              {selectedEngine.license.name}
            </div>
            {selectedEngine.license.note && (
              <div className="engine-license-popover-note">{selectedEngine.license.note}</div>
            )}
            <a
              ref={licenseLinkRef}
              className="engine-license-popover-link"
              data-testid="engine-license-popover-link"
              href={selectedEngine.license.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              View license ↗
            </a>
          </div>,
          document.body,
        )}

      {open &&
        createPortal(
          <div
            id="engine-picker-menu"
            className="model-picker-menu engine-picker-menu"
            data-testid="engine-picker-menu"
            ref={menuRef}
            style={
              menuPos
                ? {
                    top: menuPos.top,
                    bottom: menuPos.bottom,
                    left: menuPos.left,
                    width: menuPos.width,
                    maxHeight: menuPos.maxHeight,
                  }
                : { visibility: 'hidden' }
            }
          >
            <input
              ref={searchRef}
              type="search"
              className="form-input model-picker-search"
              data-testid="engine-picker-search"
              aria-label="Search engines"
              placeholder="Search engines…"
              role="combobox"
              aria-expanded={open}
              aria-controls="engine-picker-menu"
              aria-activedescendant={effectiveActiveOptionId
                ? `engine-option-${optionSlug(effectiveActiveOptionId)}`
                : undefined}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={handleSearchKeyDown}
            />
            {searching ? (
              <div className="model-picker-search-results" role="listbox">
                {searchMatches.map((engine) => renderOption(engine))}
                {searchMatches.length === 0 && (
                  <div className="model-picker-empty">No engines match “{query.trim()}”.</div>
                )}
              </div>
            ) : (
              <div className="model-picker-panes">
                <div className="model-picker-providers" role="menu">
                  {tiers.map((tier) => (
                    <button
                      key={tier.tier}
                      type="button"
                      className={`model-provider-group${tier.tier === resolvedActiveTier ? ' is-active' : ''}`}
                      data-testid={`engine-picker-tier-${tier.tier}`}
                      aria-expanded={tier.tier === resolvedActiveTier}
                      onFocus={() => setActiveTier(tier.tier)}
                      onMouseEnter={() => setActiveTier(tier.tier)}
                      onClick={() => setActiveTier(tier.tier)}
                    >
                      <span className="model-avatar" aria-hidden>{tierInitial(tier.tier)}</span>
                      <span className="model-provider-name">{engineTierLabel(tier.tier)}</span>
                      {tier.availableCount === 0 && (
                        <span className="engine-tier-unavailable" data-testid={`engine-picker-tier-${tier.tier}-unavailable`}>
                          unavailable
                        </span>
                      )}
                      <ChevronRight size={14} className="model-provider-caret" aria-hidden />
                    </button>
                  ))}
                </div>
                <div className="model-picker-models" role="listbox">
                  {ordinaryChoices.length > 0 && (
                    <div className="model-picker-empty" role="presentation">Other choices</div>
                  )}
                  {ordinaryChoices.map((choice) => renderOption(choice))}
                  {activeTierEngines.map((engine) => renderOption(engine))}
                  {ordinaryChoices.length === 0 && activeTierEngines.length === 0 && (
                    <div className="model-picker-empty">No engines in this tier.</div>
                  )}
                </div>
              </div>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
