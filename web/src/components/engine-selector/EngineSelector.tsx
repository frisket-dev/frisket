import {
  Fragment,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import { ExternalLink } from 'lucide-react';
import { createPortal } from 'react-dom';

import { useAnchoredPosition } from '../../hooks/useAnchoredPosition';
import type {
  EngineSelectorChoice,
  EngineSelectorDetailFooterContext,
  EngineSelectorGroup,
  EngineSelectorProps,
  EngineSelectorStatus,
} from './types';
import './EngineSelector.css';

const RECENT_LIMIT = 3;
const HOVER_DELAY_MS = 120;

function choiceForId(groups: readonly EngineSelectorGroup[], id: string | null): EngineSelectorChoice | null {
  if (!id) return null;
  for (const group of groups) {
    const choice = group.choices.find((candidate) => candidate.id === id);
    if (choice) return choice;
  }
  return null;
}

function groupForChoice(groups: readonly EngineSelectorGroup[], choiceId: string | null): EngineSelectorGroup | null {
  if (!choiceId) return null;
  return groups.find((group) => group.choices.some((choice) => choice.id === choiceId)) ?? null;
}

function statusLabel(status: EngineSelectorStatus): string {
  return {
    ready: 'Ready',
    needs_setup: 'Needs setup',
    working: 'Working',
    unavailable: 'Unavailable',
  }[status];
}

function groupStatus(group: EngineSelectorGroup): EngineSelectorStatus {
  if (group.choices.some((choice) => choice.status === 'ready')) return 'ready';
  if (group.choices.some((choice) => choice.status === 'needs_setup')) return 'needs_setup';
  if (group.choices.some((choice) => choice.status === 'working')) return 'working';
  return 'unavailable';
}

function recentStorageKey(namespace: string): string {
  return `frisket:engine-selector:recent:${namespace}`;
}

function readRecents(namespace: string, ids: ReadonlySet<string>): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(recentStorageKey(namespace)) ?? '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((id): id is string => typeof id === 'string' && ids.has(id)).slice(0, RECENT_LIMIT);
  } catch {
    return [];
  }
}

function saveRecent(namespace: string, choiceId: string, ids: ReadonlySet<string>): void {
  const next = [choiceId, ...readRecents(namespace, ids).filter((id) => id !== choiceId)].slice(0, RECENT_LIMIT);
  try {
    localStorage.setItem(recentStorageKey(namespace), JSON.stringify(next));
  } catch {
    // Storage is optional presentation history; selection must still work.
  }
}

/** `canRun` is the catalog's admission signal; setup-capable choices are not
 * ready until it is true. Keep the existing order inside each bucket. */
function prioritizeRunnableChoices(choices: readonly EngineSelectorChoice[]): EngineSelectorChoice[] {
  return [
    ...choices.filter((choice) => choice.canRun === true),
    ...choices.filter((choice) => choice.canRun !== true),
  ];
}

export function EngineSelector({
  label,
  groups,
  value,
  recentNamespace,
  onSelect,
  renderDetailFooter,
  notice,
  disabled = false,
  searchPlaceholder = 'Search all engines…',
  triggerRef: externalTriggerRef,
}: EngineSelectorProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  useImperativeHandle(externalTriggerRef, () => triggerRef.current!, []);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const detailFooterRef = useRef<HTMLDivElement | null>(null);
  const mobileBackRef = useRef<HTMLButtonElement | null>(null);
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pinnedIdRef = useRef<string | null>(null);
  const editingSetupRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [recentIds, setRecentIds] = useState<readonly string[]>([]);
  const [query, setQuery] = useState('');
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null);
  const [previewedId, setPreviewedId] = useState<string | null>(null);
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  const [editingSetup, setEditingSetup] = useState(false);
  const [providerFilter, setProviderFilter] = useState('');
  const [mobileDetail, setMobileDetail] = useState(false);
  const [mobileOriginId, setMobileOriginId] = useState<string | null>(null);
  const [wideAnchor, setWideAnchor] = useState(false);
  const [expandedFacts, setExpandedFacts] = useState<ReadonlySet<string>>(new Set());

  const allChoices = useMemo(() => groups.flatMap((group) => group.choices), [groups]);
  const authoritativeIds = useMemo(() => new Set(allChoices.map((choice) => choice.id)), [allChoices]);
  const selectedChoice = choiceForId(groups, value);
  const previewedChoice = choiceForId(groups, previewedId) ?? selectedChoice ?? allChoices[0] ?? null;
  const hasSearch = query.trim().length > 0;
  const isSmallCatalog = allChoices.length <= 6;
  // The centered fallback folds the provider rail into tabs. Rendering both
  // would make its two-column body overflow before CSS had a chance to hide it.
  const showGroups = wideAnchor && !hasSearch && groups.length > 1 && !isSmallCatalog;
  const currentGroup = groups.find((group) => group.id === activeGroupId)
    ?? groupForChoice(groups, previewedChoice?.id ?? value)
    ?? groups[0]
    ?? null;
  const currentChoices = (hasSearch || isSmallCatalog
    ? allChoices.filter((choice) => {
        const group = groupForChoice(groups, choice.id);
        const haystack = `${choice.label} ${choice.summary ?? ''} ${group?.label ?? ''}`.toLowerCase();
        const terms = hasSearch ? query.trim().toLowerCase().split(/\s+/) : [];
        return terms.every((term) => haystack.includes(term));
      })
    : (currentGroup?.choices ?? []).filter((choice) => {
        const term = providerFilter.trim().toLowerCase();
        return !term || `${choice.label} ${choice.summary ?? ''}`.toLowerCase().includes(term);
      }));
  const recents = recentIds.filter((id) => authoritativeIds.has(id));
  const recentOrderedChoices = hasSearch
    ? currentChoices
    : [...currentChoices].sort((left, right) => {
        const leftRecent = recents.indexOf(left.id);
        const rightRecent = recents.indexOf(right.id);
        if (leftRecent < 0 && rightRecent < 0) return 0;
        if (leftRecent < 0) return 1;
        if (rightRecent < 0) return -1;
        return leftRecent - rightRecent;
      });
  const orderedChoices = prioritizeRunnableChoices(recentOrderedChoices);
  const hasRecentChoices = !hasSearch && orderedChoices.some((choice) => recents.includes(choice.id));
  const hasNonRecentChoices = !hasSearch && orderedChoices.some((choice) => !recents.includes(choice.id));
  const showProviderFilter = !hasSearch && (currentGroup?.choices.length ?? 0) > 12;
  const positioning = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: 'right',
    width: 760,
    gap: 4,
    minHeight: 240,
  });

  const cancelHoverIntent = () => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = null;
  };

  useEffect(() => () => cancelHoverIntent(), []);

  useEffect(() => {
    pinnedIdRef.current = pinnedId;
  }, [pinnedId]);

  useLayoutEffect(() => {
    editingSetupRef.current = editingSetup;
  }, [editingSetup]);

  useEffect(() => {
    if (!mobileDetail) return undefined;
    const focus = requestAnimationFrame(() => mobileBackRef.current?.focus());
    return () => cancelAnimationFrame(focus);
  }, [mobileDetail]);

  const openSelector = () => {
    cancelHoverIntent();
    // Each opening starts from the latest committed selection, using the
    // existing preview/group fallbacks instead of a second effect render.
    setPreviewedId(null);
    setActiveGroupId(null);
    pinnedIdRef.current = null;
    editingSetupRef.current = false;
    setPinnedId(null);
    setEditingSetup(false);
    setMobileDetail(false);
    setMobileOriginId(null);
    setProviderFilter('');
    setQuery('');
    setRecentIds(readRecents(recentNamespace, authoritativeIds));
    setOpen(true);
  };

  useLayoutEffect(() => {
    if (!open) return undefined;
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (!dialog.open) dialog.showModal();
    const focus = requestAnimationFrame(() => searchRef.current?.focus());
    return () => {
      cancelAnimationFrame(focus);
      cancelHoverIntent();
      if (dialog.open) dialog.close();
      triggerRef.current?.focus();
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return undefined;
    const measure = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      setWideAnchor(Boolean(rect && window.innerWidth >= 1000 && rect.right >= 768));
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [open]);

  const close = () => {
    setOpen(false);
  };

  const explicitPreview = (choice: EngineSelectorChoice, showMobileDetail = false) => {
    cancelHoverIntent();
    setPinnedId(null);
    setEditingSetup(false);
    setPreviewedId(choice.id);
    setActiveGroupId(groupForChoice(groups, choice.id)?.id ?? null);
    if (showMobileDetail) {
      setMobileOriginId(choice.id);
      setMobileDetail(true);
    }
  };

  const previewAfterIntent = (choice: EngineSelectorChoice) => {
    if (pinnedIdRef.current || editingSetupRef.current) return;
    cancelHoverIntent();
    hoverTimerRef.current = setTimeout(() => {
      hoverTimerRef.current = null;
      if (!pinnedIdRef.current && !editingSetupRef.current) {
        setPreviewedId(choice.id);
        setActiveGroupId(groupForChoice(groups, choice.id)?.id ?? null);
      }
    }, HOVER_DELAY_MS);
  };

  const choose = (choice: EngineSelectorChoice) => {
    if (choice.status === 'unavailable' || !choice.canAuthor) {
      explicitPreview(choice);
      return;
    }
    onSelect(choice);
    saveRecent(recentNamespace, choice.id, authoritativeIds);
    setPreviewedId(choice.id);
    if (choice.status === 'ready') {
      close();
      return;
    }
    setPinnedId(choice.id);
    setEditingSetup(choice.status === 'needs_setup');
    requestAnimationFrame(() => {
      const setupControl = detailFooterRef.current?.querySelector<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      (setupControl ?? detailFooterRef.current)?.focus();
    });
  };

  const activateChoice = (choice: EngineSelectorChoice) => {
    if (window.innerWidth < 600) explicitPreview(choice, true);
    else choose(choice);
  };

  const onChoiceKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activateChoice(orderedChoices[index]);
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const next = (index + (event.key === 'ArrowDown' ? 1 : -1) + orderedChoices.length) % orderedChoices.length;
      dialogRef.current?.querySelector<HTMLButtonElement>(`[data-engine-selector-choice="${CSS.escape(orderedChoices[next].id)}"]`)?.focus();
      return;
    }
    if (event.key === 'ArrowLeft' && showGroups) {
      event.preventDefault();
      if (currentGroup) dialogRef.current?.querySelector<HTMLButtonElement>(`[data-engine-selector-group="${CSS.escape(currentGroup.id)}"]`)?.focus();
    }
  };

  const onGroupKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      dialogRef.current?.querySelector<HTMLButtonElement>('[data-engine-selector-choice]')?.focus();
      return;
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    const next = (index + (event.key === 'ArrowDown' ? 1 : -1) + groups.length) % groups.length;
    dialogRef.current?.querySelector<HTMLButtonElement>(`[data-engine-selector-group="${CSS.escape(groups[next].id)}"]`)?.focus();
  };

  const returnToMobileChoices = () => {
    const origin = mobileOriginId;
    setMobileDetail(false);
    requestAnimationFrame(() => {
      if (origin) dialogRef.current?.querySelector<HTMLButtonElement>(`[data-engine-selector-choice="${CSS.escape(origin)}"]`)?.focus();
    });
  };

  const navigateToProvider = (group: EngineSelectorGroup) => {
    cancelHoverIntent();
    pinnedIdRef.current = null;
    editingSetupRef.current = false;
    setPinnedId(null);
    setEditingSetup(false);
    setProviderFilter('');
    setActiveGroupId(group.id);
    setPreviewedId(group.choices[0]?.id ?? null);
  };

  const detailFooter: EngineSelectorDetailFooterContext | null = previewedChoice
    ? {
        choice: previewedChoice,
        pinned: pinnedId === previewedChoice.id,
        onEditingChange: setEditingSetup,
        close,
      }
    : null;
  const triggerCopy = selectedChoice ?? previewedChoice;
  const triggerActivity = triggerCopy?.activity;
  const triggerProgress = triggerActivity?.percent;
  const triggerProgressValue = typeof triggerProgress === 'number' && Number.isFinite(triggerProgress)
    ? Math.min(100, Math.max(0, triggerProgress))
    : undefined;
  const triggerSecondaryCopy = !triggerCopy
    ? label
    : triggerActivity
      ? triggerActivity.label
      : triggerCopy.status !== 'ready'
        ? triggerCopy.blocker ?? triggerCopy.summary ?? statusLabel(triggerCopy.status)
        : `${groupForChoice(groups, triggerCopy.id)?.label ?? ''}${triggerCopy.summary ? ` · ${triggerCopy.summary}` : ''}`;
  const anchoredStyle = wideAnchor && positioning
    ? { top: positioning.top, bottom: positioning.bottom, left: positioning.left, width: positioning.width, maxHeight: Math.min(420, positioning.maxHeight) }
    : undefined;

  return (
    <div className="engine-selector" data-testid="engine-selector">
      {!open && notice}
      <button
        ref={triggerRef}
        type="button"
        className="engine-selector__trigger form-input"
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={disabled}
        onClick={openSelector}
      >
        <span className={`engine-selector__status engine-selector__status--${triggerCopy?.status ?? 'unavailable'}`} aria-hidden />
        <span className="engine-selector__trigger-copy">
          <strong>{triggerCopy?.label ?? 'Choose an engine'}</strong>
          <small>{triggerSecondaryCopy}</small>
          {triggerActivity && <progress
            className="engine-selector__trigger-progress"
            aria-label={`${triggerCopy?.label ?? label}: ${triggerActivity.label}`}
            aria-valuemin={triggerProgressValue === undefined ? undefined : 0}
            aria-valuemax={triggerProgressValue === undefined ? undefined : 100}
            aria-valuenow={triggerProgressValue}
            max={100}
            value={triggerProgressValue}
          />}
        </span>
        <span aria-hidden>⌄</span>
      </button>

      {open && createPortal(
        <dialog
          ref={dialogRef}
          className={`engine-selector__dialog${wideAnchor ? ' engine-selector__dialog--anchored' : ''}${mobileDetail ? ' engine-selector__dialog--mobile-detail' : ''}`}
          aria-label={`${label} selector`}
          data-testid="engine-selector-dialog"
          style={anchoredStyle}
          onCancel={(event) => {
            event.preventDefault();
            close();
          }}
          onClose={() => {
            if (open) close();
          }}
          onSubmit={(event) => event.stopPropagation()}
        >
          <header className="engine-selector__header">
            <input
              ref={searchRef}
              type="text"
              role="searchbox"
              className="form-input engine-selector__search"
              aria-label={`Search ${label}`}
              placeholder={searchPlaceholder}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="button" className="engine-selector__close" aria-label={`Close ${label} selector`} onClick={close}>×</button>
          </header>

          {notice}

          {!showGroups && !isSmallCatalog && groups.length > 1 && !hasSearch && (
            <nav className="engine-selector__tabs" aria-label="Providers">
              {groups.map((group) => (
                <button
                  key={group.id}
                  type="button"
                  aria-pressed={currentGroup?.id === group.id}
                  onClick={() => navigateToProvider(group)}
                ><span className={`engine-selector__status engine-selector__status--${groupStatus(group)}`} aria-hidden />{group.label}</button>
              ))}
            </nav>
          )}

          <div className={`engine-selector__body${showGroups ? '' : ' engine-selector__body--flat'}`}>
            {showGroups && (
              <aside className="engine-selector__groups" aria-label="Providers">
                {groups.map((group, index) => (
                  <button
                    key={group.id}
                    type="button"
                    data-engine-selector-group={group.id}
                    className={group.id === currentGroup?.id ? 'is-active' : ''}
                    onMouseEnter={() => {
                      if (pinnedIdRef.current || editingSetupRef.current) return;
                      cancelHoverIntent();
                      hoverTimerRef.current = setTimeout(() => {
                        hoverTimerRef.current = null;
                        if (pinnedIdRef.current || editingSetupRef.current) return;
                        setActiveGroupId(group.id);
                        setPreviewedId(group.choices[0]?.id ?? null);
                        setProviderFilter('');
                      }, HOVER_DELAY_MS);
                    }}
                    onMouseLeave={cancelHoverIntent}
                    onClick={() => navigateToProvider(group)}
                    onKeyDown={(event) => onGroupKeyDown(event, index)}
                  ><span className={`engine-selector__status engine-selector__status--${groupStatus(group)}`} aria-hidden /><span>{group.label}</span></button>
                ))}
              </aside>
            )}

            <section className="engine-selector__choices" aria-label={`${label} choices`}>
              {showProviderFilter && (
                <input
                  type="text"
                  role="searchbox"
                  className="form-input engine-selector__provider-filter"
                  aria-label={`Filter ${currentGroup?.label ?? 'provider'} choices`}
                  placeholder={`Filter ${currentGroup?.label ?? 'choices'}…`}
                  value={providerFilter}
                  onChange={(event) => setProviderFilter(event.target.value)}
                />
              )}
              {orderedChoices.length === 0 ? (
                <p className="engine-selector__empty">No choices match.</p>
              ) : orderedChoices.map((choice, index) => {
                const group = groupForChoice(groups, choice.id);
                const isRecent = !hasSearch && recents.includes(choice.id);
                const isFirstNonRecent = hasRecentChoices && hasNonRecentChoices && !isRecent
                  && !orderedChoices.slice(0, index).some((candidate) => !recents.includes(candidate.id));
                return <Fragment key={choice.id}>
                  {isFirstNonRecent && <div className="engine-selector__recent-divider" data-engine-selector-recent-divider aria-hidden />}
                  <button
                    key={choice.id}
                    type="button"
                    className={`engine-selector__choice${previewedChoice?.id === choice.id ? ' is-previewed' : ''}${value === choice.id ? ' is-selected' : ''}`}
                    data-engine-selector-choice={choice.id}
                    data-choice-id={choice.id}
                    aria-current={value === choice.id ? 'true' : undefined}
                    onMouseEnter={() => previewAfterIntent(choice)}
                    onMouseLeave={cancelHoverIntent}
                    onFocus={() => explicitPreview(choice)}
                    onClick={() => activateChoice(choice)}
                    onKeyDown={(event) => onChoiceKeyDown(event, index)}
                  >
                    <span className={`engine-selector__status engine-selector__status--${choice.status}`} aria-hidden />
                    <span className="engine-selector__choice-copy">
                      <strong>{choice.label}</strong>
                      {choice.summary && <small>{choice.summary}</small>}
                    </span>
                    {!showGroups && group && <span className="engine-selector__group-tag">{group.label}</span>}
                    {choice.isDefault && <span className="engine-selector__default">Default</span>}
                    {isRecent && <span className="engine-selector__recent">Recent</span>}
                    {value === choice.id && <span aria-label="Selected">✓</span>}
                  </button>
                </Fragment>;
              })}
            </section>

            <section className="engine-selector__detail" aria-live="polite" onMouseEnter={cancelHoverIntent}>
              <div className="engine-selector__detail-scroll">
                {mobileDetail && <button ref={mobileBackRef} type="button" className="engine-selector__back" onClick={returnToMobileChoices}>← Back</button>}
                {previewedChoice ? <>
                  <div className="engine-selector__detail-heading">
                    <div>
                      <div className="engine-selector__detail-title">
                        <h2>{previewedChoice.label}</h2>
                        {previewedChoice.destination && <span className="engine-selector__destination">{previewedChoice.destination}</span>}
                      </div>
                      <span className={`engine-selector__status-copy engine-selector__status-copy--${previewedChoice.status}`}>
                        <span className={`engine-selector__status engine-selector__status--${previewedChoice.status}`} aria-hidden />
                        {statusLabel(previewedChoice.status)}
                      </span>
                    </div>
                    {previewedChoice.modelCardUrl && <a href={previewedChoice.modelCardUrl} target="_blank" rel="noopener noreferrer">Model card <ExternalLink size={12} aria-hidden /></a>}
                  </div>
                  {previewedChoice.description && <p>{previewedChoice.description}</p>}
                  {previewedChoice.facts && previewedChoice.facts.length > 0 && (
                    <dl className="engine-selector__facts">
                      {previewedChoice.facts.map((fact) => {
                        const factKey = `${previewedChoice.id}:${fact.label}`;
                        const expanded = expandedFacts.has(factKey);
                        const list = Array.isArray(fact.value) ? fact.value : null;
                        return <div key={factKey}>
                          <dt>{fact.label}</dt>
                          <dd>{list ? <>
                            <button
                              type="button"
                              aria-expanded={expanded}
                              onClick={() => setExpandedFacts((current) => {
                                const next = new Set(current);
                                if (next.has(factKey)) next.delete(factKey);
                                else next.add(factKey);
                                return next;
                              })}
                            >{list.length} {expanded ? '▴' : '▾'}</button>
                            {expanded && <span className="engine-selector__chips">{list.map((item) => <span key={item}>{item}</span>)}</span>}
                          </> : String(fact.value)}</dd>
                        </div>;
                      })}
                    </dl>
                  )}
                  {previewedChoice.blocker && <p className="engine-selector__blocker">{previewedChoice.blocker}</p>}
                </> : <p className="engine-selector__empty">No choices are available.</p>}
                  {previewedChoice && (renderDetailFooter || (mobileDetail && previewedChoice.canAuthor && previewedChoice.status !== 'unavailable')) && (
                    <footer ref={detailFooterRef} tabIndex={-1} className="engine-selector__footer">
                      {renderDetailFooter?.(detailFooter!)}
                      {mobileDetail && previewedChoice.canAuthor && previewedChoice.status !== 'unavailable' && <button type="button" className="primary-button" onClick={() => choose(previewedChoice)}>{previewedChoice.status === 'needs_setup' ? 'Select and set up' : 'Select'}</button>}
                    </footer>
                  )}
              </div>
            </section>
          </div>
        </dialog>,
        document.body,
      )}
    </div>
  );
}
