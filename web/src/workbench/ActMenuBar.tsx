// Menu bar: the compact density of the SAME ResolvedActTab[] model rendered by
// ActRibbon. Tabs remain adaptive — Misc,
// contextual tabs, plugin actions, and plugin launchers appear in both
// densities — while groups are flattened only for this dropdown presentation.

import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { PanelTopOpen } from 'lucide-react';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import overflowStyles from './ActTabsOverflow.module.css';
import {
  actItemsInDisplayOrder,
  type ActMenuCommand,
  type ResolvedActItem,
  type ResolvedActTab,
} from './actSurface';

// Keyboard-shortcut badges are compact-density presentation only (the ribbon
// does not render them). They remain cosmetic, not global key handlers.
const ACTION_MENU_SHORTCUTS: Readonly<Record<string, string>> = {
  'media.ocr': '⌥O',
};

// Empty since the legacy help-shortcuts menu command retired; kept as the
// lookup seam for any future command badge.
const COMMAND_MENU_SHORTCUTS: Partial<Record<ActMenuCommand, string>> = {};

const contributionSlug = (contributionId: string) =>
  contributionId.replace(/[^a-zA-Z0-9]+/g, '-');

const itemId = (item: ResolvedActItem) =>
  item.kind === 'action'
    ? item.launcherKind
    : item.kind === 'command'
      ? item.command
      : item.contributionId;

const itemTestId = (item: ResolvedActItem) =>
  item.kind === 'action'
    ? `menu-action-${item.launcherKind}`
    : item.kind === 'command'
      ? `menu-command-${item.command}`
      : `menu-launcher-${contributionSlug(item.contributionId)}`;

const itemDataAttrs = (item: ResolvedActItem) => {
  const shared = {
    'data-act-item-kind': item.kind,
    'data-act-item-id': itemId(item),
  };
  if (item.kind === 'action') {
    return { ...shared, 'data-launcher-kind': item.launcherKind };
  }
  if (item.kind === 'launcher') {
    return {
      ...shared,
      'data-contribution-id': item.contributionId,
      'data-runtime-source': 'runtimeIndex',
    };
  }
  return shared;
};

const itemShortcut = (item: ResolvedActItem) =>
  item.kind === 'action'
    ? ACTION_MENU_SHORTCUTS[item.launcherKind]
    : item.kind === 'command'
      ? COMMAND_MENU_SHORTCUTS[item.command]
      : undefined;

/** The one open dropdown, rendered once (not per-trigger) and anchored to
 *  whichever trigger is currently open. Mounting fresh when `tab.id` changes
 *  lets a hover-switch re-subscribe the popover lifecycle and re-measure from
 *  the newly active trigger. */
function ActMenuDropdown({
  tab,
  triggerRef,
  onClose,
  renderItem,
}: {
  tab: ResolvedActTab;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onClose(): void;
  renderItem(item: ResolvedActItem): ReactNode;
}) {
  const popoverRef = useRef<HTMLDivElement>(null);
  // Escape + outside-pointerdown; any `.act-menubar-trigger` is ignored so a
  // trigger's own click keeps sole ownership of switching/toggling menus.
  useNativePopover(popoverRef, onClose, { ignoreSelector: '.act-menubar-trigger' });
  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: true,
    align: 'left',
    width: 224,
    gap: 4,
  });

  return (
    <div
      ref={popoverRef}
      id={`act-menubar-dropdown-${tab.id}`}
      className="menu-pop act-menubar-pop"
      data-testid={`menubar-dropdown-${tab.id}`}
      style={
        menuPos
          ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
          : { position: 'fixed', visibility: 'hidden' }
      }
    >
      {tab.groups.flatMap((group) => actItemsInDisplayOrder(group.items)).map(renderItem)}
    </div>
  );
}

export function ActMenuBar({
  tabs,
  onRunAction,
  onCommand,
  onLaunchContribution,
  onExpandRibbon,
}: {
  tabs: ResolvedActTab[];
  onRunAction(launcherKind: string): void;
  onCommand(command: ActMenuCommand): void;
  onLaunchContribution(contributionId: string): void;
  onExpandRibbon(): void;
}) {
  const [openTabId, setOpenTabId] = useState<string | null>(null);
  // Focus returns to the current trigger on Escape, outside click, or item
  // activation. The ref follows hover-switching, rather than remembering only
  // the trigger that received the first click.
  const openTriggerRef = useRef<HTMLButtonElement | null>(null);

  const restoreTriggerFocus = () => {
    openTriggerRef.current?.focus();
    openTriggerRef.current = null;
  };

  const closeMenu = () => {
    setOpenTabId(null);
    restoreTriggerFocus();
  };

  // An adaptive/contextual tab can disappear while its dropdown is open.
  // Normalize only the compact renderer's transient open state; selection
  // normalization for both densities belongs to the parent.
  useEffect(() => {
    if (openTabId !== null && !tabs.some((tab) => tab.id === openTabId)) {
      const staleTabId = openTabId;
      // Defer the state write outside the synchronous effect body while the
      // missing `openTab` lookup already keeps the stale dropdown unmounted.
      const timer = window.setTimeout(() => {
        setOpenTabId((current) => (current === staleTabId ? null : current));
        openTriggerRef.current = null;
      }, 0);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [tabs, openTabId]);

  const activateItem = (item: ResolvedActItem) => {
    closeMenu();
    if (item.kind === 'action') onRunAction(item.launcherKind);
    else if (item.kind === 'command') onCommand(item.command);
    else onLaunchContribution(item.contributionId);
  };

  const renderItem = (item: ResolvedActItem) => {
    const shortcut = itemShortcut(item);
    return (
      <button
        type="button"
        className="menu-item"
        key={`${item.kind}:${itemId(item)}`}
        data-testid={itemTestId(item)}
        {...itemDataAttrs(item)}
        onClick={() => activateItem(item)}
      >
        <item.Icon size={13} className="menu-item-icon" />
        <span className="menu-item-name">{item.label}</span>
        {shortcut && <span className="menu-item-kbd">{shortcut}</span>}
      </button>
    );
  };

  const openTab = tabs.find((tab) => tab.id === openTabId) ?? null;

  return (
    <div className="act-menubar" data-testid="act-menubar" data-ribbon-mode="menu">
      <div className={`act-menubar-menus ${overflowStyles.menuTabs}`}>
        {tabs.map((tab) => (
          <div className={`act-menubar-menu ${overflowStyles.menuTab}`} key={tab.id}>
            <button
              type="button"
              className={`act-menubar-trigger${openTabId === tab.id ? ' open' : ''}${
                tab.contextual
                  ? ` contextual${tab.accent ? ` contextual-${tab.accent}` : ''}`
                  : ''
              }`}
              data-testid={`menubar-menu-${tab.id}`}
              data-contextual={tab.contextual ? 'true' : 'false'}
              aria-expanded={openTabId === tab.id}
              aria-controls={openTabId === tab.id ? `act-menubar-dropdown-${tab.id}` : undefined}
              onClick={(event) => {
                openTriggerRef.current = event.currentTarget;
                setOpenTabId((current) => (current === tab.id ? null : tab.id));
              }}
              onMouseEnter={(event) => {
                // Native menu-bar gesture: while any menu is open, hovering a
                // sibling trigger switches to it without another click.
                if (openTabId !== null && openTabId !== tab.id) {
                  openTriggerRef.current = event.currentTarget;
                  setOpenTabId(tab.id);
                }
              }}
            >
              {tab.label}
            </button>
          </div>
        ))}
      </div>
      {openTab && (
        <ActMenuDropdown
          key={openTab.id}
          tab={openTab}
          triggerRef={openTriggerRef}
          onClose={closeMenu}
          renderItem={renderItem}
        />
      )}
      <button
        type="button"
        className={`act-menubar-expand ${overflowStyles.menuExpand}`}
        data-testid="ribbon-expand"
        onClick={onExpandRibbon}
        title="Expand the ribbon"
      >
        <PanelTopOpen size={13} /> Ribbon
      </button>
    </div>
  );
}
