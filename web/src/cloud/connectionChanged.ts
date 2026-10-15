/** Mounted model selectors need fresh routing facts after Cloud setup elsewhere. */
export const CLOUD_CONNECTION_CHANGED = 'frisket:cloud-connection-changed';

export function notifyCloudConnectionChanged(): void {
  window.dispatchEvent(new Event(CLOUD_CONNECTION_CHANGED));
}
