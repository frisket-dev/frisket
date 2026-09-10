export const ScopePanel = ({ React, ctx }) => {
  return React.createElement('section', {
    'data-testid': 'demo-sidebar-panel',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-placement-host': ctx?.placement?.host ?? 'missing',
    'data-sheet-name': ctx?.sheet?.name ?? 'missing',
  }, 'demo sidebar panel');
};
