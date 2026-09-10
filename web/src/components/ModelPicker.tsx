// Braintrust-style model picker: a searchable menu grouped by provider, each
// provider expanding to its models, plus a "Configure AI providers" entry that
// NAVIGATES to the workspace AI-providers settings page — configuration lives in
// Settings, not in a popover pane.
//
// The live /api/providers catalog is the source of truth in the local tier; if
// it is unavailable (e.g. the hosted tier, which has no local daemon), the
// picker falls back to the static model list — and the hosted fallback drops
// the unusable "free local Ollama" option.

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { ChevronRight, Settings } from 'lucide-react';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';

import { listOrgLocalEndpoints, listProviders, providerStatus } from '../api/open';
import type {
  LocalProviderCatalog,
  LocalProviderEntry,
  LocalProviderModel,
} from '../api/types';
import { PROVIDER_LABELS, modelOptionsForTier, modelProviderId, providerIsUsable } from '../actions/model';
import { navigate } from '../routes';
import { StatusChip } from './PanelPrimitives';
import { LocalServerGuidance, localServerGuidanceState } from './LocalServerGuidance';

const optionSlug = (value: string): string =>
  value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

const providerIdentity = (provider: LocalProviderEntry): string =>
  provider.kind === 'local_http' ? provider.endpoint_id : provider.id;

function fallbackCatalog(hosted: boolean): LocalProviderCatalog {
  const byProvider = new Map<string, LocalProviderModel[]>();
  for (const option of modelOptionsForTier(hosted)) {
    const provider = modelProviderId(option.id);
    if (provider === 'ollama') continue;
    const models = byProvider.get(provider) ?? [];
    models.push({ id: option.id, label: option.label, price: null, local: provider === 'ollama' });
    byProvider.set(provider, models);
  }
  return {
    schemaVersion: 'frisket.providers.fallback',
    tier: 'local',
    providers: [...byProvider.entries()].map(([id, models]) => ({
      id,
      label: PROVIDER_LABELS[id] ?? id,
      kind: 'platform_api' as const,
      models,
      configured: false,
      source: null,
      hint: null,
    })),
  };
}

// Catalog labels embed a description after an em/en dash, e.g.
// "Gemini 2.5 Flash — fast/cheap (default)". The picker shows the name as the
// primary line and the dash suffix as the quiet note.
function modelName(label: string): string {
  return label.split(/\s+[—–-]\s+/)[0].trim();
}

function modelNote(label: string): string {
  const parts = label.split(/\s+[—–-]\s+/);
  return parts.length > 1 ? parts.slice(1).join(' · ').trim() : '';
}

function providerInitial(label: string): string {
  return (label.trim()[0] ?? '·').toUpperCase();
}

function providerAffordanceCopy(provider: LocalProviderEntry): string {
  return provider.kind === 'local_http'
    ? `Check ${provider.label} or add a model…`
    : `Add ${provider.label} API key…`;
}

// Hosted-picker seam: the hosted tier has no /api/providers workspace
// daemon, so its catalog is normally the static fallback with 'ollama'
// filtered out (modelOptionsForTier drops it). When an operator HAS wired
// up team local-model endpoints (GET /api/org/local-endpoints), their models
// appear here under the endpoint's stable id and label, with every model id
// qualified by that endpoint.
// Settings-only pull affordance: this entry never sets
// `pull_enabled`/`installed_models`/etc, so no guidance/download UI renders
// here even when LocalServerGuidance's empty-state classifier runs against
// it -- deliberately: that Download flow lives in the org settings card, not
// the picker.
/** Resolves the picker's catalog for a given mount/recheck: the live
 *  /api/providers catalog (falling back to the static list on failure), with
 *  the org local-models group merged in when hosted and present. Kept as a
 *  single async function (no setState inside) so both the mount effect and
 *  the guidance panel's Recheck button share one code path instead of two
 *  copies that could drift on the merge logic. */
async function loadHostedMergedCatalog(hosted: boolean): Promise<LocalProviderCatalog> {
  const base = await listProviders().catch((error) => {
    const status = (error as { status?: number })?.status;
    return fallbackCatalog(hosted || status === 404);
  });
  if (!hosted) return base;
  const orgCatalog = await listOrgLocalEndpoints().catch(() => null);
  const entries = orgCatalog?.endpoints ?? [];
  if (entries.length === 0) return base;
  const identities = new Set(entries.map(providerIdentity));
  return {
    ...base,
    providers: [
      ...base.providers.filter(
        (provider) => !identities.has(providerIdentity(provider)),
      ),
      ...entries,
    ],
  };
}

interface SelectedModelInfo {
  name: string;
  note: string;
  providerLabel: string | null;
}

export interface ModelPickerChoice {
  id: string;
  label: string;
  note?: string;
  available?: boolean;
  unavailableReason?: string | null;
}

export interface ModelPickerChoiceGroup {
  id: string;
  label: string;
  choices: ModelPickerChoice[];
}

interface PickerOption {
  id: string;
  name: string;
  note: string;
  available: boolean;
  unavailableReason: string | null;
}

const pickerOptionForModel = (
  model: LocalProviderModel,
  available: boolean,
  unavailableReason: string | null,
): PickerOption => ({
  id: model.id,
  name: modelName(model.label),
  note: modelNote(model.label),
  available,
  unavailableReason,
});

const pickerOptionForChoice = (choice: ModelPickerChoice): PickerOption => ({
  id: choice.id,
  name: choice.label,
  note: choice.note ?? '',
  available: choice.available !== false,
  unavailableReason: choice.unavailableReason ?? null,
});

function selectedModelInfo(
  catalog: LocalProviderCatalog | null,
  modelId: string,
  choiceGroups: ModelPickerChoiceGroup[],
): SelectedModelInfo {
  for (const group of choiceGroups) {
    const choice = group.choices.find((candidate) => candidate.id === modelId);
    if (choice) {
      return {
        name: choice.label,
        note: choice.note ?? '',
        providerLabel: group.label,
      };
    }
  }
  for (const provider of catalog?.providers ?? []) {
    for (const model of provider.models) {
      if (model.id === modelId) {
        return {
          name: modelName(model.label),
          note: modelNote(model.label),
          providerLabel: provider.label,
        };
      }
    }
  }
  return { name: modelId, note: '', providerLabel: null };
}

interface ModelPickerProps {
  value: string;
  onChange: (modelId: string) => void;
  /** Optional non-model leaves for actions whose execution choices include
   *  fixed engines beside provider models. They join this one picker without
   *  changing the action's engine/model wire contract. */
  choiceGroups?: ModelPickerChoiceGroup[];
  /** Authority for provider-model leaves when the containing action has a
   *  separately catalogued model engine. */
  providerModelsAvailable?: boolean;
  providerModelsUnavailableReason?: string | null;
  searchPlaceholder?: string;
  emptySearchLabel?: string;
  /** Best-effort tier hint for the static fallback; the live catalog wins. */
  hosted?: boolean;
  /** id of an external label element. When set, the trigger button announces
   *  as "<label> <selected model>" (the APG select-only-combobox naming
   *  pattern: aria-labelledby references the label AND the value element) —
   *  a bare aria-label would drop the current selection from the name. */
  ariaLabelledBy: string;
}

export function ModelPicker({
  value,
  onChange,
  choiceGroups = [],
  providerModelsAvailable = true,
  providerModelsUnavailableReason = null,
  searchPlaceholder = 'Search models…',
  emptySearchLabel = 'No models match',
  hosted = false,
  ariaLabelledBy,
}: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [catalog, setCatalog] = useState<LocalProviderCatalog | null>(null);
  const [activeProvider, setActiveProvider] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  // Extracted so the local-server guidance panel's Recheck button can
  // re-run the exact same probe->setCatalog->fallback(->org-merge) path the
  // mount effect uses, instead of duplicating the error handling.
  const fetchCatalog = () => loadHostedMergedCatalog(hosted).then(setCatalog);
  const recheckLocalServer = (providerId: string) => hosted
    ? fetchCatalog()
    : providerStatus(providerId).then((entry) => {
        setCatalog((current) => current && ({
          ...current,
          providers: current.providers.map((provider) => (
            providerIdentity(provider) === providerIdentity(entry) ? entry : provider
          )),
        }));
      });

  useEffect(() => {
    let cancelled = false;
    loadHostedMergedCatalog(hosted).then((next) => {
      if (!cancelled) setCatalog(next);
    });
    return () => {
      cancelled = true;
    };
  }, [hosted]);

  // The menu is portaled to <body> with fixed positioning so it escapes the
  // action panel's `overflow: hidden` (which otherwise clipped it behind the
  // bottom dock). Anchor its right edge to the button and extend leftward so a
  // right-docked picker stays on-screen; the shared flip predicate still opens
  // it upward near the footer (there the space below is cramped).
  const menuPos = useAnchoredPosition(buttonRef, {
    enabled: open,
    align: 'right',
    width: (_rect, viewportWidth) => Math.min(520, viewportWidth - 16),
    gap: 4,
    minHeight: 220,
  });

  // Native Popover API lifecycle. The menu is a `popover="manual"` element, so
  // showPopover() puts it in the TOP LAYER — it paints above any later overlay
  // with no z-index management. Manual mode (not auto) is deliberate: it grants
  // no light-dismiss and, crucially, ignores Escape natively, which is exactly
  // the old `escape: false` on this surface (Escape in the search input must not
  // dismiss). We drive it imperatively because the element is portaled and
  // conditionally rendered, and set the attribute here rather than in JSX
  // because stable @types/react (18.3) still lacks the `popover` prop (canary
  // only). React's `autoFocus` no-ops on a popover (it is display:none until
  // shown), so focus the search input explicitly once the popover is visible.
  useLayoutEffect(() => {
    const el = menuRef.current;
    if (!open || !el) return undefined;
    if (el.getAttribute('popover') !== 'manual') el.setAttribute('popover', 'manual');
    if (el.isConnected && !el.matches(':popover-open')) el.showPopover();
    // Focus the search after the popover is in the top layer. showPopover() runs
    // the UA focusing steps (which leave focus on the invoking trigger for a
    // non-autofocus popover), so move it on the next frame to win that ordering.
    const raf = requestAnimationFrame(() => searchRef.current?.focus());
    return () => {
      cancelAnimationFrame(raf);
      if (el.isConnected && el.matches(':popover-open')) el.hidePopover();
    };
  }, [open]);

  // Explicit outside-pointerdown dismissal — manual mode provides none. rootRef
  // covers the trigger, menuRef the portaled popover; a pointerdown in either is
  // "inside" and does not close. Ignoring the trigger here is also what keeps
  // its toggle from double-firing: the pointerdown never dismisses, so the
  // button's own onClick performs the single open/close transition. No Escape
  // listener exists (escape:false is now structural via popover=manual).
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (rootRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [open]);

  // Memoized so its identity is stable across renders when catalog doesn't
  // change — `catalog?.providers ?? []` would otherwise mint a fresh empty
  // array (and thus a new reference) on every render whenever catalog is
  // null, defeating searchMatches' memoization below.
  const providers = useMemo(() => catalog?.providers ?? [], [catalog]);
  const searching = query.trim().length > 0;
  // The static fallback catalog carries no configured/reachable facts at
  // all -- see the identical guard beside `activeUsable` below. Computed
  // once here so both searchMatches and the per-section pane agree on what
  // "unusable" means for the current catalog snapshot.
  const isFallbackCatalog = catalog === null || catalog.schemaVersion === 'frisket.providers.fallback';

  // Flat, provider-tagged matches for search mode. Tokens and the match check
  // live inside the memo so the only inputs eslint needs to track are
  // providers/query — no externally-captured function to go stale. An
  // unusable provider's models are excluded here too: search must not be a
  // back door around the per-section "Add ... API key…" affordance below.
  const searchMatches = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const choices = choiceGroups.flatMap((group) => group.choices.flatMap((choice) => {
      const haystack = `${group.label} ${choice.id} ${choice.label} ${choice.note ?? ''}`.toLowerCase();
      return tokens.every((token) => haystack.includes(token))
        ? [pickerOptionForChoice(choice)]
        : [];
    }));
    const models = providers.flatMap((provider) => {
      if (!isFallbackCatalog && !providerIsUsable(provider)) return [];
      const matches: PickerOption[] = [];
      for (const model of provider.models) {
        const haystack = `${provider.label} ${model.id} ${model.label}`.toLowerCase();
        if (tokens.every((token) => haystack.includes(token))) {
          matches.push(pickerOptionForModel(
            model,
            providerModelsAvailable,
            providerModelsUnavailableReason,
          ));
        }
      }
      return matches;
    });
    return [...choices, ...models];
  }, [
    choiceGroups,
    providers,
    query,
    isFallbackCatalog,
    providerModelsAvailable,
    providerModelsUnavailableReason,
  ]);

  // The provider whose models fill the right pane: the last hovered/focused one,
  // else the current value's provider, else the first provider.
  const choiceGroupIdentity = (group: ModelPickerChoiceGroup): string => `choice-${group.id}`;
  const resolvedActive =
    (activeProvider && (
      providers.some((p) => providerIdentity(p) === activeProvider)
      || choiceGroups.some((group) => choiceGroupIdentity(group) === activeProvider)
    ) ? activeProvider : null) ??
    (() => {
      const group = choiceGroups.find((item) => (
        item.choices.some((choice) => choice.id === value)
      ));
      return group ? choiceGroupIdentity(group) : undefined;
    })() ??
    (() => {
      const provider = providers.find((item) => item.models.some((model) => model.id === value));
      return provider ? providerIdentity(provider) : undefined;
    })() ??
    (choiceGroups[0] ? choiceGroupIdentity(choiceGroups[0]) : undefined) ??
    (providers[0] ? providerIdentity(providers[0]) : undefined) ??
    null;
  const activeChoiceGroup = choiceGroups.find(
    (group) => choiceGroupIdentity(group) === resolvedActive,
  ) ?? null;
  const activeEntry = providers.find((p) => providerIdentity(p) === resolvedActive) ?? null;
  const activeModels = activeEntry?.models ?? [];
  // Keep every provider visible; unusable providers show configuration instead of models.
  // Do not clear an already-selected value.
  const activeUsable = activeEntry ? isFallbackCatalog || providerIsUsable(activeEntry) : true;

  const selectModel = (modelId: string) => {
    onChange(modelId);
    // Focus-return: the selected option (or search input) was the last-focused
    // element inside the popover, so send focus back to the trigger on close —
    // the native-popover a11y contract, here made deterministic across unmount.
    const returnFocus = Boolean(menuRef.current?.contains(document.activeElement));
    setOpen(false);
    setQuery('');
    if (returnFocus) requestAnimationFrame(() => buttonRef.current?.focus());
  };

  // Where provider keys are actually configured: workspace keys in the local
  // tier, org keys under a hosted identity. Shared by the footer "Configure
  // AI providers…" row and each unusable section's own affordance row so the
  // two links can't drift on the destination.
  const goToProviderSettings = () => {
    setOpen(false);
    navigate(
      hosted
        ? { kind: 'settings', scope: 'organization', section: 'ai-providers' }
        : { kind: 'settings', scope: 'personal', section: 'ai-providers' },
    );
  };

  const renderProviderAffordance = (provider: LocalProviderEntry) => (
    <button
      type="button"
      className="model-picker-provider-affordance"
      data-testid={`model-picker-add-key-${providerIdentity(provider)}`}
      onClick={goToProviderSettings}
    >
      {providerAffordanceCopy(provider)}
    </button>
  );

  const renderOption = (option: PickerOption) => {
    return (
      <button
        key={option.id}
        type="button"
        className={`model-option ${option.id === value ? 'is-selected' : ''}`}
        data-testid={`model-option-${optionSlug(option.id)}`}
        data-tour="model-option"
        data-model-id={option.id}
        role="option"
        aria-selected={option.id === value}
        aria-disabled={!option.available}
        disabled={!option.available}
        title={!option.available ? option.unavailableReason ?? 'Unavailable' : undefined}
        onClick={() => selectModel(option.id)}
      >
        <span className="model-option-text">
          <span className="model-option-label">{option.name}</span>
          {option.note && <span className="model-option-note">{option.note}</span>}
          {!option.available && (
            <span className="model-option-note">{option.unavailableReason ?? 'Unavailable'}</span>
          )}
        </span>
        {option.id === value && <span className="model-option-check" aria-hidden>✓</span>}
      </button>
    );
  };

  const selectedChoice = choiceGroups
    .flatMap((group) => group.choices)
    .find((choice) => choice.id === value);
  const selectedUnavailable = selectedChoice
    ? selectedChoice.available === false
    : !providerModelsAvailable;
  const selectedUnavailableReason = selectedChoice
    ? selectedChoice.unavailableReason
    : providerModelsUnavailableReason;
  const selected = selectedModelInfo(catalog ?? fallbackCatalog(hosted), value, choiceGroups);
  const selectedSub = [selected.providerLabel, selected.note].filter(Boolean).join(' · ');
  const selectedValueId = `${ariaLabelledBy}-value`;
  // Only claim a model is "ready" once the live catalog has actually loaded
  // and confirmed it: the static fallback (schemaVersion
  // 'frisket.providers.fallback') carries no configured/reachable facts, so
  // it would otherwise read as "nothing configured" on every slow network.
  const noProviderConfigured =
    catalog !== null &&
    catalog.schemaVersion !== 'frisket.providers.fallback' &&
    !selectedChoice &&
    !providers.some(providerIsUsable);

  return (
    <div className="model-picker" ref={rootRef}>
      <button
        type="button"
        ref={buttonRef}
        className={`form-input model-picker-button${noProviderConfigured ? ' is-unconfigured' : ''}`}
        data-testid="model-picker-button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={`${ariaLabelledBy} ${selectedValueId}`}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span className="model-avatar" aria-hidden>
          {noProviderConfigured ? '!' : providerInitial(selected.providerLabel ?? selected.name)}
        </span>
        {noProviderConfigured ? (
          <span
            className="model-picker-selected"
            id={selectedValueId}
            data-testid="model-picker-no-provider"
          >
            <span className="model-picker-selected-name">No provider configured</span>
            <span className="model-picker-selected-sub">Add a key in Settings to run this action</span>
          </span>
        ) : (
          <span className="model-picker-selected" id={selectedValueId}>
            <span className="model-picker-selected-name">
              {selected.name}
              {selectedUnavailable && (
                <span className="engine-tier-unavailable"> · unavailable</span>
              )}
            </span>
            {selectedSub && <span className="model-picker-selected-sub">{selectedSub}</span>}
          </span>
        )}
      </button>

      {selectedUnavailable && (
        <p className="engine-selected-unavailable-reason">
          {selected.name} can’t run here —{' '}
          <code>{selectedUnavailableReason ?? 'Unavailable'}</code>
        </p>
      )}

      {open &&
        createPortal(
          <div
            className="model-picker-menu"
            data-testid="model-picker-menu"
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
                  data-testid="model-picker-search"
                  aria-label={searchPlaceholder.replace(/…$/, '')}
                  placeholder={searchPlaceholder}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
                {searching ? (
                  <div className="model-picker-search-results" role="listbox">
                    {searchMatches.map(renderOption)}
                    {searchMatches.length === 0 && (
                      <div className="model-picker-empty">{emptySearchLabel} “{query.trim()}”.</div>
                    )}
                  </div>
                ) : (
                  <div className="model-picker-panes">
                    <div className="model-picker-providers" role="menu">
                      {choiceGroups.map((group) => (
                        <button
                          key={choiceGroupIdentity(group)}
                          type="button"
                          className={`model-provider-group ${choiceGroupIdentity(group) === resolvedActive ? 'is-active' : ''}`}
                          data-testid={`model-provider-group-${optionSlug(choiceGroupIdentity(group))}`}
                          data-tour="model-provider-group"
                          aria-expanded={choiceGroupIdentity(group) === resolvedActive}
                          onFocus={() => setActiveProvider(choiceGroupIdentity(group))}
                          onMouseEnter={() => setActiveProvider(choiceGroupIdentity(group))}
                          onClick={() => setActiveProvider(choiceGroupIdentity(group))}
                        >
                          <span className="model-avatar" aria-hidden>{providerInitial(group.label)}</span>
                          <span className="model-provider-name">{group.label}</span>
                          {!group.choices.some((choice) => choice.available !== false) && (
                            <span className="engine-tier-unavailable">unavailable</span>
                          )}
                          <ChevronRight size={14} className="model-provider-caret" aria-hidden />
                        </button>
                      ))}
                      {providers.map((provider) => (
                        <button
                          key={providerIdentity(provider)}
                          type="button"
                          className={`model-provider-group ${providerIdentity(provider) === resolvedActive ? 'is-active' : ''}`}
                          data-testid={`model-provider-group-${providerIdentity(provider)}`}
                          data-tour="model-provider-group"
                          data-provider-id={providerIdentity(provider)}
                          aria-expanded={providerIdentity(provider) === resolvedActive}
                          onFocus={() => setActiveProvider(providerIdentity(provider))}
                          onMouseEnter={() => setActiveProvider(providerIdentity(provider))}
                          onClick={() => setActiveProvider(providerIdentity(provider))}
                        >
                          <span className="model-avatar" aria-hidden>{providerInitial(provider.label)}</span>
                          <span className="model-provider-name">{provider.label}</span>
                          {provider.kind === 'local_http' && (
                            <StatusChip
                              tone={provider.reachable ? 'info' : 'neutral'}
                              testId={`local-server-reachability-${provider.endpoint_id}`}
                            >
                              {provider.reachable ? 'reachable' : 'offline'}
                            </StatusChip>
                          )}
                          {!providerModelsAvailable && (
                            <span className="engine-tier-unavailable">unavailable</span>
                          )}
                          <ChevronRight size={14} className="model-provider-caret" aria-hidden />
                        </button>
                      ))}
                    </div>
                    <div className="model-picker-models" role="listbox">
                      {activeChoiceGroup ? (
                        <>
                          {activeChoiceGroup.choices.map((choice) => (
                            renderOption(pickerOptionForChoice(choice))
                          ))}
                          {activeChoiceGroup.choices.length === 0 && (
                            <div className="model-picker-empty">No choices available.</div>
                          )}
                        </>
                      ) : activeEntry && !activeUsable ? (() => {
                        // Unusable section: never list its models (this is
                        // the "Add Anthropic API key…" spec). Ollama is the
                        // one provider with a richer dead-end story than "add
                        // a key" -- prefer the existing install/pull guidance
                        // whenever the live catalog carries the probe facts
                        // it needs (reachable/protocol/installed_models); the
                        // static fallback catalog (hosted tier, or the live
                        // probe failed) has none of that, so it falls back to
                        // the same plain affordance row every other unusable
                        // provider gets.
                        const isLiveOllama =
                          activeEntry.kind === 'local_http' &&
                          catalog?.schemaVersion !== 'frisket.providers.fallback';
                        const guidanceState = isLiveOllama
                          ? localServerGuidanceState(activeEntry)
                          : null;
                        if (activeEntry.kind === 'local_http' && guidanceState) {
                          return (
                            <LocalServerGuidance
                              entry={activeEntry}
                              variant="compact"
                              onRecheck={() => recheckLocalServer(activeEntry.endpoint_id)}
                            />
                          );
                        }
                        return renderProviderAffordance(activeEntry);
                      })() : (
                        <>
                          {activeModels.map((model) => renderOption(pickerOptionForModel(
                            model,
                            providerModelsAvailable,
                            providerModelsUnavailableReason,
                          )))}
                          {activeModels.length === 0 && (
                            <div className="model-picker-empty">No models available.</div>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                )}
            <button
              type="button"
              className="model-picker-configure"
              data-testid="configure-providers"
              onClick={goToProviderSettings}
            >
              <Settings size={13} /> Configure AI providers…
            </button>
          </div>,
          document.body,
        )}
    </div>
  );
}
