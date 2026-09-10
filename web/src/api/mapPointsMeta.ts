// Map-points response metadata side-channel.
//
// The projection-status panel (WorkbenchProjectionStatusPanel) used to be fed
// by the first-party MapView's onProjectionStatus prop. The map view is now a
// runtime plugin that reads Arrow bytes through ctx.projection.fetchData —
// which deliberately returns ONLY the bytes (no HTTP headers) — so the host
// mirrors every map-points response's header metadata here, at the API layer
// where the headers exist, regardless of which surface initiated the fetch.

export interface MapPointsResponseMeta {
  sheetId: string;
  columnId: string;
  validPoints: number;
  transient: boolean;
  generation: string;
  schema: string;
  backend: string;
}

type MapPointsMetaListener = (meta: MapPointsResponseMeta) => void;

const listeners = new Set<MapPointsMetaListener>();

export function subscribeMapPointsMeta(listener: MapPointsMetaListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function emitMapPointsMeta(meta: MapPointsResponseMeta): void {
  for (const listener of listeners) {
    listener(meta);
  }
}
