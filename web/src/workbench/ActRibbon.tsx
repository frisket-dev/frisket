// Ribbon: the expanded density of the SAME ResolvedActTab[] model as the menu
// bar. Adaptive promotions (contextual tabs,
// Misc, plugins, and plugin launchers) therefore stay identical across both
// renderers. Every action is configure-first: clicking opens the ActionPanel
// pre-targeted, it never runs.

import { PanelTopClose } from 'lucide-react';
import overflowStyles from './ActTabsOverflow.module.css';
import {
  actItemsInDisplayOrder,
  type ActMenuCommand,
  type ResolvedActItem,
  type ResolvedActTab,
} from './actSurface';

const contributionSlug = (contributionId: string) =>
  contributionId.replace(/[^a-zA-Z0-9]+/g, '-');

const itemKey = (item: ResolvedActItem) =>
  item.kind === 'action'
    ? item.launcherKind
    : item.kind === 'command'
      ? item.command
      : item.contributionId;

const itemTestId = (item: ResolvedActItem) =>
  item.kind === 'action'
    ? `ribbon-action-${item.launcherKind}`
    : item.kind === 'command'
      ? `ribbon-command-${item.command}`
      : `ribbon-launcher-${contributionSlug(item.contributionId)}`;

const itemDataAttrs = (item: ResolvedActItem) => {
  const shared = {
    'data-act-item-kind': item.kind,
    'data-act-item-id': itemKey(item),
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

export function ActRibbon({
  tabs,
  activeTabId,
  onSelectTab,
  onRunAction,
  onCommand,
  onLaunchContribution,
  onCollapse,
}: {
  tabs: ResolvedActTab[];
  activeTabId: string;
  onSelectTab(tabId: string): void;
  onRunAction(launcherKind: string): void;
  onCommand(command: ActMenuCommand): void;
  /** Reveal a plugin launcher's primary placement (LIBRARY group). */
  onLaunchContribution(contributionId: string): void;
  onCollapse(): void;
}) {
  const onItemClick = (item: ResolvedActItem) => {
    if (item.kind === 'action') onRunAction(item.launcherKind);
    else if (item.kind === 'command') onCommand(item.command);
    else onLaunchContribution(item.contributionId);
  };
  const activeTab = tabs.find((tab) => tab.id === activeTabId) ?? tabs[0] ?? null;
  return (
    <div className="act-ribbon" data-testid="act-ribbon" data-ribbon-mode="ribbon">
      <div
        className={`act-ribbon-tabstrip ${overflowStyles.ribbonTabstrip}`}
        role="tablist"
        aria-label="Action tabs"
      >
        <div className={`act-ribbon-tabs ${overflowStyles.ribbonTabs}`}>
          {tabs.map((tab, index) => {
            const previous = tabs[index - 1];
            const needsDivider = tab.contextual && (!previous || !previous.contextual);
            return (
              <div className="act-ribbon-tab-wrap" key={tab.id}>
                {needsDivider && <span className="act-ribbon-tab-divider" aria-hidden />}
                <button
                  type="button"
                  role="tab"
                  aria-selected={activeTab?.id === tab.id}
                  className={`act-ribbon-tab${activeTab?.id === tab.id ? ' active' : ''}${
                    tab.contextual ? ` contextual contextual-${tab.accent}` : ''
                  }`}
                  data-testid={`ribbon-tab-${tab.id}`}
                  data-contextual={tab.contextual ? 'true' : 'false'}
                  onClick={() => onSelectTab(tab.id)}
                >
                  {tab.contextual && <span className="act-ribbon-tab-dot" aria-hidden />}
                  {tab.label}
                </button>
              </div>
            );
          })}
        </div>
        <button
          type="button"
          className={`act-ribbon-collapse ${overflowStyles.ribbonCollapse}`}
          data-testid="ribbon-collapse"
          onClick={onCollapse}
          title="Collapse ribbon to menu bar"
        >
          <PanelTopClose size={13} /> Collapse ribbon
        </button>
      </div>
      <div className="act-ribbon-band" data-testid="act-ribbon-band">
        {activeTab?.groups.map((group, groupIndex) => (
          <div className="act-ribbon-group" key={`${group.caption}-${groupIndex}`}>
            {groupIndex > 0 && <span className="act-ribbon-group-divider" aria-hidden />}
            <div className="act-ribbon-group-body">
              {actItemsInDisplayOrder(group.items).flatMap((item) =>
                item.primary
                  ? [
                      <button
                        type="button"
                        className="act-ribbon-primary"
                        key={itemKey(item)}
                        data-testid={itemTestId(item)}
                        {...itemDataAttrs(item)}
                        onClick={() => onItemClick(item)}
                      >
                        <span className="act-ribbon-primary-tile">
                          <item.Icon size={16} />
                        </span>
                        <span className="act-ribbon-primary-label">{item.label}</span>
                      </button>,
                    ]
                  : [],
              )}
              <div className="act-ribbon-secondaries">
                {actItemsInDisplayOrder(group.items).flatMap((item) =>
                  !item.primary
                    ? [
                        <button
                          type="button"
                          className="act-ribbon-secondary"
                          key={itemKey(item)}
                          data-testid={itemTestId(item)}
                          {...itemDataAttrs(item)}
                          onClick={() => onItemClick(item)}
                        >
                          <item.Icon size={13} />
                          <span>{item.label}</span>
                        </button>,
                      ]
                    : [],
                )}
              </div>
            </div>
            <div className="act-ribbon-group-caption">{group.caption}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
