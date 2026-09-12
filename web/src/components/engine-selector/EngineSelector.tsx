import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import { ExternalLink } from 'lucide-react';

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

export function EngineSelector({
  label,
  groups,
  value,
  recentNamespace,
  onSelect,
  renderDetailFooter,
  disabled = false,
  searchPlaceholder = 'Search all engines…',
}: EngineSelectorProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const detailFooterRef = useRef<HTMLDivElement | null>(null);
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const restoreFocusRef = useRef(false);
  const pinnedIdRef = useRef<string | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null);
  const [previewedId, setPreviewedId] = useState<string | null>(null);
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  const [editingSetup, setEditingSetup] = useState(false);
  const [providerFilter, setProviderFilter] = useState('');
  const [mobileDetail, setMobileDetail] = useState(false);
  const [wideAnchor, setWideAnchor] = useState(false);
  const [expandedFacts, setExpandedFacts] = useState<ReadonlySet<string>>(new Set());

  const allChoices = useMemo(() => groups.flatMap((group) => group.choices), [groups]);
  const authoritativeIds = useMemo(() => new Set(allChoices.map((choice) => choice.id)), [allChoices]);
  const selectedChoice = choiceForId(groups, value);
  const previewedChoice = choiceForId(groups, previewedId) ?? selectedChoice ?? allChoices[0] ?? null;
  const hasSearch = query.trim().length > 0;
  // The centered fallback folds the provider rail into tabs. Rendering both
  // would make its two-column body overflow before CSS had a chance to hide it.
  const showGroups = wideAnchor && !hasSearch && groups.length > 1 && allChoices.length > 6;
  const currentGroup = groups.find((group) => group.id === activeGroupId)
    ?? groupForChoice(groups, previewedChoice?.id ?? value)
    ?? groups[0]
    ?? null;
  const currentChoices = (hasSearch
    ? allChoices.filter((choice) => {
        const group = groupForChoice(groups, choice.id);
        const haystack = `${choice.label} ${choice.summary ?? ''} ${group?.label ?? ''}`.toLowerCase();
        return query.trim().toLowerCase().split(/\s+/).every((term) => haystack.includes(term));
      })
    : (currentGroup?.choices ?? []).filter((choice) => {
        const term = providerFilter.trim().toLowerCase();
        return !term || `${choice.label} ${choice.summary ?? ''}`.toLowerCase().includes(term);
      }));
  const recents = open ? readRecents(recentNamespace, authoritativeIds) : [];
  const orderedChoices = hasSearch
    ? currentChoices
    : [...currentChoices].sort((left, right) => {
        const leftRecent = recents.indexOf(left.id);
        const rightRecent = recents.indexOf(right.id);
        if (leftRecent < 0 && rightRecent < 0) return 0;
        if (leftRecent < 0) return 1;
        if (rightRecent < 0) return -1;
        return leftRecent - rightRecent;
      });
  const showProviderFilter = !hasSearch && (currentGroup?.choices.length ?? 0) > 12;
  const positioning = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: 'right',
    width: 760,
    gap: 4,
    minHeight: 240,
  });

  useEffect(() => () => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
  }, []);

  useEffect(() => {
    pinnedIdRef.current = pinnedId;
  }, [pinnedId]);

  useEffect(() => {
    if (!open && restoreFocusRef.current) {
      restoreFocusRef.current = false;
      triggerRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const first = selectedChoice ?? allChoices[0] ?? null;
    setPreviewedId(first?.id ?? null);
    setActiveGroupId(groupForChoice(groups, first?.id ?? null)?.id ?? groups[0]?.id ?? null);
    setPinnedId(null);
    setEditingSetup(false);
    setMobileDetail(false);
    setProviderFilter('');
  // Opening is the boundary where committed selection becomes the preview.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return undefined;
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (!dialog.open) dialog.showModal();
    const focus = requestAnimationFrame(() => searchRef.current?.focus());
    return () => {
      cancelAnimationFrame(focus);
      if (dialog.open) dialog.close();
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
    restoreFocusRef.current = true;
    setOpen(false);
  };

  const explicitPreview = (choice: EngineSelectorChoice, showMobileDetail = false) => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    setPinnedId(null);
    setEditingSetup(false);
    setPreviewedId(choice.id);
    setActiveGroupId(groupForChoice(groups, choice.id)?.id ?? null);
    if (showMobileDetail) setMobileDetail(true);
  };

  const previewAfterIntent = (choice: EngineSelectorChoice) => {
    if (pinnedIdRef.current || hoverTimerRef.current) return;
    hoverTimerRef.current = setTimeout(() => {
      hoverTimerRef.current = null;
      if (!pinnedIdRef.current && !editingSetup) {
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
    requestAnimationFrame(() => detailFooterRef.current?.focus());
  };

  const onChoiceKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      choose(event.currentTarget.dataset.choiceId ? orderedChoices[index] : previewedChoice!);
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const next = (index + (event.key === 'ArrowDown' ? 1 : -1) + orderedChoices.length) % orderedChoices.length;
      document.querySelector<HTMLButtonElement>(`[data-engine-selector-choice="${CSS.escape(orderedChoices[next].id)}"]`)?.focus();
      return;
    }
    if (event.key === 'ArrowLeft' && showGroups) {
      event.preventDefault();
      document.querySelector<HTMLButtonElement>('[data-engine-selector-group]')?.focus();
    }
  };

  const detailFooter: EngineSelectorDetailFooterContext | null = previewedChoice
    ? {
        choice: previewedChoice,
        pinned: pinnedId === previewedChoice.id,
        regionRef: detailFooterRef,
        onEditingChange: setEditingSetup,
      }
    : null;
  const triggerCopy = selectedChoice ?? previewedChoice;
  const anchoredStyle = wideAnchor && positioning
    ? { top: positioning.top, bottom: positioning.bottom, left: positioning.left, width: positioning.width, maxHeight: Math.min(420, positioning.maxHeight) }
    : undefined;

  return (
    <div className="engine-selector" data-testid="engine-selector">
      <button
        ref={triggerRef}
        type="button"
        className="engine-selector__trigger form-input"
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <span className={`engine-selector__status engine-selector__status--${triggerCopy?.status ?? 'unavailable'}`} aria-hidden />
        <span className="engine-selector__trigger-copy">
          <strong>{triggerCopy?.label ?? 'Choose an engine'}</strong>
          <small>{triggerCopy ? `${groupForChoice(groups, triggerCopy.id)?.label ?? ''}${triggerCopy.summary ? ` · ${triggerCopy.summary}` : ''}` : label}</small>
        </span>
        <span aria-hidden>⌄</span>
      </button>

      {open && (
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
        >
          <header className="engine-selector__header">
            <input
              ref={searchRef}
              type="search"
              className="form-input engine-selector__search"
              aria-label={`Search ${label}`}
              placeholder={searchPlaceholder}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="button" className="engine-selector__close" aria-label={`Close ${label} selector`} onClick={close}>×</button>
          </header>

          {!wideAnchor && groups.length > 1 && !hasSearch && (
            <nav className="engine-selector__tabs" aria-label="Providers">
              {groups.map((group) => (
                <button
                  key={group.id}
                  type="button"
                  aria-pressed={currentGroup?.id === group.id}
                  onClick={() => {
                    setActiveGroupId(group.id);
                    setPreviewedId(group.choices[0]?.id ?? null);
                    setPinnedId(null);
                  }}
                >{group.label}</button>
              ))}
            </nav>
          )}

          <div className="engine-selector__body">
            {showGroups && (
              <aside className="engine-selector__groups" aria-label="Providers">
                {groups.map((group) => (
                  <button
                    key={group.id}
                    type="button"
                    data-engine-selector-group={group.id}
                    className={group.id === currentGroup?.id ? 'is-active' : ''}
                    onMouseEnter={() => {
                      if (pinnedIdRef.current) return;
                      hoverTimerRef.current = setTimeout(() => {
                        setActiveGroupId(group.id);
                        setPreviewedId(group.choices[0]?.id ?? null);
                      }, HOVER_DELAY_MS);
                    }}
                    onClick={() => {
                      setActiveGroupId(group.id);
                      setPreviewedId(group.choices[0]?.id ?? null);
                      setPinnedId(null);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'ArrowRight') {
                        event.preventDefault();
                        document.querySelector<HTMLButtonElement>('[data-engine-selector-choice]')?.focus();
                      }
                    }}
                  >{group.label}</button>
                ))}
              </aside>
            )}

            <section className="engine-selector__choices" aria-label={`${label} choices`}>
              {showProviderFilter && (
                <input
                  type="search"
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
                return (
                  <button
                    key={choice.id}
                    type="button"
                    className={`engine-selector__choice${previewedChoice?.id === choice.id ? ' is-previewed' : ''}${value === choice.id ? ' is-selected' : ''}`}
                    data-engine-selector-choice={choice.id}
                    data-choice-id={choice.id}
                    aria-current={value === choice.id ? 'true' : undefined}
                    onMouseEnter={() => previewAfterIntent(choice)}
                    onFocus={() => explicitPreview(choice)}
                    onClick={() => window.innerWidth < 600 ? explicitPreview(choice, true) : choose(choice)}
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
                );
              })}
            </section>

            <section className="engine-selector__detail" aria-live="polite">
              <div className="engine-selector__detail-scroll">
                {mobileDetail && <button type="button" className="engine-selector__back" onClick={() => setMobileDetail(false)}>← Back</button>}
                {previewedChoice ? <>
                  <div className="engine-selector__detail-heading">
                    <div>
                      <h2>{previewedChoice.label}</h2>
                      <span className={`engine-selector__status-copy engine-selector__status-copy--${previewedChoice.status}`}>
                        <span className={`engine-selector__status engine-selector__status--${previewedChoice.status}`} aria-hidden />
                        {statusLabel(previewedChoice.status)}
                      </span>
                    </div>
                    {previewedChoice.modelCardUrl && <a href={previewedChoice.modelCardUrl} target="_blank" rel="noopener noreferrer">Model card <ExternalLink size={12} aria-hidden /></a>}
                  </div>
                  {previewedChoice.description && <p>{previewedChoice.description}</p>}
                  {previewedChoice.destination && <p className="engine-selector__destination">{previewedChoice.destination}</p>}
                  {previewedChoice.facts && previewedChoice.facts.length > 0 && (
                    <dl className="engine-selector__facts">
                      {previewedChoice.facts.map((fact) => {
                        const factKey = `${previewedChoice.id}:${fact.label}`;
                        const expanded = expandedFacts.has(factKey);
                        const list = Array.isArray(fact.value) ? fact.value : null;
                        return <div key={factKey}>
                          <dt>{fact.label}</dt>
                          <dd>{list ? (expanded ? <span className="engine-selector__chips">{list.map((item) => <span key={item}>{item}</span>)}</span> : <button type="button" onClick={() => setExpandedFacts((current) => new Set([...current, factKey]))}>{list.length} options</button>) : String(fact.value)}</dd>
                        </div>;
                      })}
                    </dl>
                  )}
                  {previewedChoice.blocker && <p className="engine-selector__blocker">{previewedChoice.blocker}</p>}
                </> : <p className="engine-selector__empty">No choices are available.</p>}
              </div>
              {previewedChoice && <footer ref={detailFooterRef} tabIndex={-1} className="engine-selector__footer">
                {renderDetailFooter?.(detailFooter!)}
                {mobileDetail && previewedChoice.canAuthor && previewedChoice.status !== 'unavailable' && <button type="button" className="primary-button" onClick={() => choose(previewedChoice)}>{previewedChoice.status === 'needs_setup' ? 'Select and set up' : 'Select'}</button>}
              </footer>}
            </section>
          </div>
        </dialog>
      )}
    </div>
  );
}
