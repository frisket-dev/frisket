export function MapView({ React, contributionId, pluginId, ctx }) {
  return React.createElement(
    'section',
    {
      'data-testid': 'trusted-local-smoke-map-plugin-ui',
      'data-contribution-id': contributionId,
      'data-plugin-id': pluginId,
      'data-loaded-from': 'local-plugin-module',
      'data-schema': ctx?.schemaVersion ?? 'missing',
      'data-sheet-name': ctx?.sheet?.name ?? 'missing',
      'data-row-count': String(ctx?.sheet?.rowCount ?? ''),
      'data-can-open-row': String(typeof ctx?.navigation?.openRow === 'function'),
      className: 'trusted-local-smoke-map-plugin-ui',
    },
    React.createElement('strong', null, 'Trusted local plugin UI'),
    React.createElement('span', null, 'frisket.geosmoke real local smoke'),
  );
}
