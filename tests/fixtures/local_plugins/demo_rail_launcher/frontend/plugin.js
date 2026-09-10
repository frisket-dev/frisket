export const DockTab = ({ React, ctx }) => {
  return React.createElement('section', {
    'data-testid': 'demo-rail-launcher-panel',
    'data-schema': ctx?.schemaVersion ?? 'missing',
  }, 'demo rail launcher target');
};
