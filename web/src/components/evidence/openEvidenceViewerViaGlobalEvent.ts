// A component file may only export components (react-refresh/only-export-components).
//
// The default `onOpen` a CitationChip uses when a caller doesn't override it
// — App.tsx listens for 'frisket:open-evidence' and opens EvidenceViewer via
// openEvidenceViewer/openEvidenceViewerForWorkspace.
export function openEvidenceViewerViaGlobalEvent(linkId: string): void {
  window.dispatchEvent(
    new CustomEvent('frisket:open-evidence', {
      detail: { evidenceLinkId: linkId },
    }),
  );
}
