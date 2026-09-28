import {
  ChevronLeft,
  ChevronRight,
  type LucideIcon,
} from 'lucide-react';
import { type ComponentType } from 'react';
import { OverflowRow } from '../components/OverflowRow';
import { useResizable } from '../components/useResizable';
import { ResizeSeam } from '../components/ResizeSeam';

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
  const renderTab = (tab: DiscoverTabDescriptor, measuring: boolean) => {
    const Icon = tab.Icon;
    const active = tab.id === activeTab;
    if (measuring) {
      return (
        <span className={`discover-tab${active ? ' discover-tab-active' : ''}`}>
          <Icon size={13} aria-hidden />
          <span>{tab.label}</span>
        </span>
      );
    }
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
        <Icon size={13} aria-hidden />
        <span>{tab.label}</span>
      </button>
    );
  };

  return (
    <div className="discover-tabrow">
      <OverflowRow
        items={tabs}
        getKey={(tab) => tab.id}
        keepVisibleKey={activeTab}
        tabListLabel="Discover tabs"
        renderItem={renderTab}
        renderOverflowItem={(tab, closeMenu) => {
          const Icon = tab.Icon;
          return (
            <button
              type="button"
              role="menuitem"
              className="menu-item discover-overflow-item"
              data-testid={`discover-tab-menu-${tab.slug}`}
              data-active={tab.id === activeTab ? 'true' : undefined}
              onClick={() => {
                onSelectTab(tab.id);
                closeMenu();
              }}
            >
              <Icon size={14} aria-hidden />
              <span>{tab.label}</span>
            </button>
          );
        }}
        className="discover-tabs"
        triggerClassName="discover-overflow-btn"
        menuClassName="discover-overflow-menu"
        triggerTestId="discover-tab-overflow"
        menuTestId="discover-tab-overflow-menu"
        overflowLabel="More Discover tabs"
        overflowTitle="More tabs"
        menuAriaLabel="More Discover tabs"
        menuWidth={148}
        menuGap={2}
      />

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
