// The open project's workspace listens for this event and shows the run in
// Review; a component file may only export components, so it lives here.
export const OPEN_RUN_REVIEW_EVENT = 'frisket:open-run-review';

export function openRunReview(runId: string): void {
  window.dispatchEvent(new CustomEvent(OPEN_RUN_REVIEW_EVENT, { detail: { runId } }));
}
