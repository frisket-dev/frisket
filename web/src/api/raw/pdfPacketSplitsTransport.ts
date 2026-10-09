export function requestPdfPacketSplit(
  url: string,
  init: RequestInit = {},
): Promise<Response> {
  // This raw boundary is replaced by httpContract once the backend's generated
  // PDF packet operations land in the integration branch.
  // eslint-disable-next-line no-restricted-syntax
  return fetch(url, init);
}
