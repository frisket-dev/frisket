export const DockTab = ({ React, ctx }) => {
  return React.createElement('section', {
    'data-testid': 'demo-dock-tab-panel',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-dock-active': String(ctx?.dock?.isActiveTab ?? false),
    'data-sheet-name': ctx?.sheet?.name ?? 'missing',
    'data-selected-count': String(ctx?.selection?.selectedCount ?? -1),
  }, React.createElement('button', {
    type: 'button',
    'data-testid': 'demo-dock-tab-focus',
    onClick: () => ctx?.dock?.focus(),
  }, 'focus dock tab'));
};
