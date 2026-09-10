// The single module that owns deck.gl's named exports, dynamically imported by
// the plugin host.library.deckgl injection (web/src/workbench/pluginContextFragments.ts)
// so deck.gl's bytes ship in exactly ONE lazily-loaded chunk. The map surface
// itself lives in the bundled frisket.geo plugin
// (src/frisket/authoring/bundled_plugins/frisket.geo/), which drives the imperative `Deck`
// class through ctx.libs.deckgl — no @deck.gl/react, no second React tree, no
// second copy of deck.gl anywhere.
export { Deck, WebMercatorViewport } from '@deck.gl/core';
export { HeatmapLayer } from '@deck.gl/aggregation-layers';
export { BitmapLayer, ScatterplotLayer } from '@deck.gl/layers';
export { TileLayer } from '@deck.gl/geo-layers';

/** The module's own shape — what `import('./deckglNamespace')` resolves to.
 *  ctx.libs.deckgl types against this. */
export type DeckglNamespace = typeof import('./deckglNamespace');
