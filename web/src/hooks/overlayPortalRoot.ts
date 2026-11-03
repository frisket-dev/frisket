/** Keep nested floating surfaces inside their owning dialog for dismissal and focus. */
export function overlayPortalRoot(anchor: Element | null): Element {
  return anchor?.closest('dialog, [role="dialog"]') ?? document.body;
}
