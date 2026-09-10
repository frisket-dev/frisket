// The bundled geo plugin contributes the Map view and
// the role-`map_points` projection contribution, authored through
// @frisket/plugin-sdk and installed into every project through the bundled
// install path ({"kind": "bundled", "value": "frisket.geo"}).
//
// `frisket plugin build .` compiles this into plugin.json +
// workbench-descriptors.json and frontend/plugin.tsx into frontend/plugin.js.
import {
  capability,
  definePlugin,
  defineProjection,
  defineView,
  needs,
} from './.frisket-sdk/index.mjs';

export default definePlugin({
  id: 'frisket.geo',
  version: '1.0.0',
  views: [
    defineView({
      key: 'map',
      title: 'Map',
      icon: 'MapPin',
      component: 'MapView',
      // Projection view family: status/build/fetchData against the
      // map_points projection the plugin also contributes below.
      projectionKind: 'frisket.geo.projection.map_points',
      placements: [
        {
          host: 'mainView',
          mode: 'pane',
          slot: 'work.companion',
          placementId: 'frisket-geo-map-companion',
          default: true,
          order: 20,
        },
      ],
      requires: [
        capability('projection.status'),
        capability('projection.data.read'),
        capability('grid.filter.applyBbox'),
        capability('grid.state.read'),
        capability('host.navigation.openRow'),
        capability('host.library.deckgl'),
      ],
      needs: [needs.activeSheet(), needs.sheetHasColumnType('geo_point')],
    }),
  ],
  projections: [
    defineProjection({
      key: 'map_points',
      title: 'Map points projection',
      handler: 'map_points',
      role: 'map_points',
    }),
  ],
  capabilities: ['plugin:trusted_local_backend'],
});
