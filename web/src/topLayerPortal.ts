/**
 * Return a portal root that remains interactive while an app modal is open.
 *
 * `showModal()` makes everything outside the active dialog inert. Native
 * popovers still belong in the browser top layer, but portaling them into the
 * dialog also makes them descendants of that modal instead of inert siblings.
 */
export function topLayerPortalRoot(): Element {
  try {
    const modals = document.querySelectorAll<HTMLDialogElement>('dialog:modal');
    return modals.item(modals.length - 1) ?? document.body;
  } catch {
    // Older DOM implementations (including some test environments) do not
    // understand the :modal pseudo-class.
    return document.body;
  }
}
