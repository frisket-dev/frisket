import {
  ChevronLeft,
  ChevronRight,
  ChevronsRight,
  type LucideIcon,
} from 'lucide-react';
import { useLayoutEffect, useRef, useState, type ComponentType } from 'react';
import { useResizable } from '../components/useResizable';
import { ResizeSeam } from '../components/ResizeSeam';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';

const DISCOVER_MIN_WIDTH = 220;
const DISCOVER_MAX_WIDTH = 520;
const DISCOVER_DEFAULT_WIDTH = 290;
const DISCOVER_RAIL_WIDTH = 48;

/** A Discover tab: the four first-party tabs (Filter · Sources · Watches ·
 *  Embeddings) plus any plugin leftSidebar panel re-homed here as an appended
 *  tab. `id` is the first-party tab name (Filter retains the internal id
 *  `Facets`) or the plugin panel's contribution id; `slug` is its testid-safe
 *  form. */
export interface DiscoverTabDescriptor {
  id: string;
  label: string;
  slug: string;
  Icon: LucideIcon;
}

export interface DiscoverPanelProps {
  projectId: string;
  open: boolean;
  /** First-party tabs first, then plugin panel tabs (already in order). */
  tabs: DiscoverTabDescriptor[];
  /** Active tab id (a first-party name or a plugin contribution id). */
  activeTab: string;
  /** Switch tabs within the open panel. */
  onSelectTab(tabId: string): void;
  /** Collapse the panel to the rail. */
  onCollapse(): void;
  /** Expand the rail (to the current tab). */
  onExpand(): void;
  /** Expand the rail to a specific tab (rail icon click). */
  onOpenTab(tabId: string): void;
  /** Renders the active tab's contribution body (host-owned dispatch). A real
   *  component (not a `renderTabBody(tabId)` closure) so the JSX call site
   *  is a genuine `<TabBody tabId={...} />` tag, not a `no-render-in-render`
   *  inline function call. */
  TabBody: ComponentType<{ tabId: string }>;
}

/**
 * The Discover tab strip. Tabs that do not fit the panel width collapse into a
 * `»` overflow menu (Chrome-devtools style); the active tab is always kept
 * visible. Active tab carries the full teal accent (background + text +
 * underline) per the Discover region tokens.
 */
function DiscoverTabStrip({
  tabs,
  activeTab,
  onSelectTab,
  onCollapse,
}: {
  tabs: DiscoverTabDescriptor[];
  activeTab: string;
  onSelectTab(tabId: string): void;
  onCollapse(): void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  // visibleCount is DOM-MEASUREMENT state, not a copy of tabs.length: the
  // overflow split is computed from live offsetWidth/clientWidth in the
  // useLayoutEffect below (recompute), so it cannot be derived during render.
  // tabs.length is only the pre-measurement seed for the first paint;
  // recompute() re-runs on every tabs change (that effect's dep is [tabs]) and
  // fires synchronously before paint, so the seed never shows stale to a user.
  // no-derived-useState here is a false positive — allowlisted in doctor.config.ts.
  const [visibleCount, setVisibleCount] = useState(tabs.length);
  const [menuOpen, setMenuOpen] = useState(false);
  const overflowTriggerRef = useRef<HTMLButtonElement>(null);
  const overflowMenuRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const row = rowRef.current;
    const measurer = measureRef.current;
    if (!row || !measurer) return;
    const OVERFLOW_BTN = 34;
    const recompute = () => {
      const widths = Array.from(measurer.children).map((el) => (el as HTMLElement).offsetWidth);
      const available = row.clientWidth;
      const total = widths.reduce((sum, w) => sum + w, 0);
      if (total <= available) {
        setVisibleCount(tabs.length);
        return;
      }
      // Overflow exists: reserve room for the `»` button and fit as many as we can.
      let used = 0;
      let count = 0;
      for (let i = 0; i < widths.length; i++) {
        used += widths[i];
        if (used + OVERFLOW_BTN <= available) count += 1;
        else break;
      }
      setVisibleCount(Math.max(1, Math.min(tabs.length, count)));
    };
    const observer = new ResizeObserver(recompute);
    observer.observe(row);
    recompute();
    return () => observer.disconnect();
    // Re-measure when the tab set changes (a plugin installs/uninstalls, or a
    // tab is hidden/revealed), so the overflow split tracks the live tabs.
  }, [tabs]);

  // Keep the active tab visible: if it falls into the overflow set, swap it in
  // for the last otherwise-visible tab.
  let visibleTabs = tabs.slice(0, visibleCount);
  let overflowTabs = tabs.slice(visibleCount);
  const activeDescriptor = tabs.find((tab) => tab.id === activeTab);
  if (activeDescriptor && overflowTabs.some((tab) => tab.id === activeTab)) {
    const displaced = visibleTabs[visibleTabs.length - 1];
    visibleTabs = [...visibleTabs.slice(0, -1), activeDescriptor];
    overflowTabs = [displaced, ...overflowTabs.filter((tab) => tab.id !== activeTab)];
  }

  // Top-layer popover: the DiscoverTabStrip `»` overflow menu has its own
  // backdrop and shares no DOM/behavior with the genuine resident
  // DiscoverPanel. useNativePopover's default escape:true/outside:true
  // replaces BOTH the old Escape-only hook AND the hand-rolled
  // `.discover-overflow-backdrop` click-catcher that used to own
  // outside-click dismissal — one mechanism instead of two.
  useNativePopover(overflowMenuRef, () => setMenuOpen(false), {
    enabled: menuOpen,
    ignoreSelector: '[data-testid="discover-tab-overflow"]',
  });
  // Fixed-position anchor now that the menu is a top-layer element — it no
  // longer inherits placement from `.discover-overflow`'s CSS positioned
  // ancestor (styles.css `.discover-overflow-menu`, right-aligned under the
  // trigger).
  const overflowMenuPos = useAnchoredPosition(overflowTriggerRef, {
    enabled: menuOpen,
    align: 'right',
    width: 148,
    gap: 2,
  });

  const renderTab = (tab: DiscoverTabDescriptor) => {
    const Icon = tab.Icon;
    const active = tab.id === activeTab;
    return (
      <button
        key={tab.id}
        type="button"
        role="tab"
        aria-selected={active}
        className={`discover-tab${active ? ' discover-tab-active' : ''}`}
        data-testid={`discover-tab-${tab.slug}`}
        onClick={() => onSelectTab(tab.id)}
      >
        <Icon size={13} />
        <span>{tab.label}</span>
      </button>
    );
  };

  return (
    <div className="discover-tabrow" role="tablist" aria-label="Discover tabs">
      {/* Hidden measurer: natural-width copies used to compute overflow. */}
      <div className="discover-tab-measure" aria-hidden ref={measureRef}>
        {tabs.map((tab) => {
          const Icon = tab.Icon;
          return (
            <span key={tab.id} className="discover-tab">
              <Icon size={13} />
              <span>{tab.label}</span>
            </span>
          );
        })}
      </div>

      <div className="discover-tabs" ref={rowRef}>
        {visibleTabs.map(renderTab)}
        {overflowTabs.length > 0 && (
          <div className="discover-overflow">
            <button
              type="button"
              ref={overflowTriggerRef}
              className={`discover-overflow-btn${menuOpen ? ' discover-overflow-btn-open' : ''}`}
              data-testid="discover-tab-overflow"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label="More Discover tabs"
              title="More tabs"
              onClick={() => setMenuOpen((open) => !open)}
            >
              <ChevronsRight size={16} />
            </button>
            {menuOpen && (
              <div
                ref={overflowMenuRef}
                className="discover-overflow-menu"
                role="menu"
                data-testid="discover-tab-overflow-menu"
                style={
                  overflowMenuPos
                    ? { position: 'fixed', inset: 'auto', top: overflowMenuPos.top, bottom: overflowMenuPos.bottom, left: overflowMenuPos.left, right: 'auto', width: overflowMenuPos.width, margin: 0 }
                    : { position: 'fixed', visibility: 'hidden' }
                }
              >
                {overflowTabs.map((tab) => {
                  const Icon = tab.Icon;
                  return (
                    <button
                      key={tab.id}
                      type="button"
                      role="menuitem"
                      className={`discover-overflow-item${tab.id === activeTab ? ' discover-overflow-item-active' : ''}`}
                      data-testid={`discover-tab-menu-${tab.slug}`}
                      onClick={() => {
                        onSelectTab(tab.id);
                        setMenuOpen(false);
                      }}
                    >
                      <Icon size={14} />
                      <span>{tab.label}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      <button
        type="button"
        className="discover-collapse"
        data-testid="discover-collapse"
        aria-label="Collapse Discover panel"
        title="Collapse Discover"
        onClick={onCollapse}
      >
        <ChevronRight size={16} />
      </button>
    </div>
  );
}

/**
 * Discover region — panel ⇄ rail. A right-edge tabbed panel (Filter · Sources
 * · Watches · Embeddings, plus any plugin leftSidebar panels re-homed as
 * appended tabs) that hosts the re-homed panel contributions, collapsible to
 * a 48px icon rail. It renders side by side with the Inspect Detail column
 * (they coexist).
 */
export function DiscoverPanel({
  projectId,
  open,
  tabs,
  activeTab,
  onSelectTab,
  onCollapse,
  onExpand,
  onOpenTab,
  TabBody,
}: DiscoverPanelProps) {
  const { width, resizing, onResizeStart, onResizeKeyDown } = useResizable({
    storageKey: `frisket:discover-width:${projectId}`,
    minWidth: DISCOVER_MIN_WIDTH,
    maxWidth: DISCOVER_MAX_WIDTH,
    defaultWidth: DISCOVER_DEFAULT_WIDTH,
    handleEdge: 'left',
  });

  if (!open) {
    return (
      <nav
        className="discover-rail"
        style={{ width: DISCOVER_RAIL_WIDTH }}
        data-testid="discover-rail"
        aria-label="Discover rail"
      >
        <button
          type="button"
          className="discover-rail-btn discover-rail-expand"
          data-testid="discover-rail-expand"
          aria-label="Expand Discover panel"
          title="Expand Discover"
          onClick={onExpand}
        >
          <ChevronLeft size={16} />
        </button>
        {tabs.map((tab) => {
          const Icon = tab.Icon;
          return (
            <button
              key={tab.id}
              type="button"
              className="discover-rail-btn discover-rail-icon"
              data-testid={`discover-rail-icon-${tab.slug}`}
              aria-label={`Open ${tab.label}`}
              title={tab.label}
              onClick={() => onOpenTab(tab.id)}
            >
              <Icon size={16} />
            </button>
          );
        })}
      </nav>
    );
  }

  return (
    <aside
      className={`discover-panel${resizing ? ' discover-panel-resizing' : ''}`}
      style={{ width }}
      data-testid="discover-panel"
      aria-label="Discover panel"
    >
      <ResizeSeam
        className="discover-seam"
        ariaLabel="Resize the Discover panel"
        testId="discover-seam"
        width={width}
        min={DISCOVER_MIN_WIDTH}
        max={DISCOVER_MAX_WIDTH}
        onResizeStart={onResizeStart}
        onResizeKeyDown={onResizeKeyDown}
      />
      <DiscoverTabStrip tabs={tabs} activeTab={activeTab} onSelectTab={onSelectTab} onCollapse={onCollapse} />
      <div
        className="discover-body"
        data-testid="discover-body"
        role="tabpanel"
        data-active-tab={activeTab}
      >
        <TabBody tabId={activeTab} />
      </div>
    </aside>
  );
}
