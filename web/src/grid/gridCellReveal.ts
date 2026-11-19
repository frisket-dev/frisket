interface GridCellRevealRequest {
  requestId: number;
  projectId: string;
  sheetId: string;
  rowId: string;
  /** Both null scrolls the row into view without selecting a cell or taking
   *  focus: a selected cell turns the next click on it into an edit. */
  columnId: string | null;
  columnName: string | null;
}

type Listener = () => void;

let nextRequestId = 1;
let pendingRequest: GridCellRevealRequest | null = null;
const listeners = new Set<Listener>();

export function requestGridCellReveal(
  request: Omit<GridCellRevealRequest, 'requestId'>,
): GridCellRevealRequest {
  pendingRequest = { ...request, requestId: nextRequestId++ };
  for (const listener of listeners) listener();
  return pendingRequest;
}

export function consumeGridCellReveal(requestId: number): void {
  if (pendingRequest?.requestId !== requestId) return;
  pendingRequest = null;
  for (const listener of listeners) listener();
}

export function getPendingGridCellReveal(): GridCellRevealRequest | null {
  return pendingRequest;
}

export function subscribeGridCellReveal(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
