/** Actions without a drawer: collection expansion remains available through
 * its URL handler, saved actions, and Copilot. */
export function hasNoActionDrawer(canonicalKind: string): boolean {
  return canonicalKind === 'derive.collection_expand';
}

/** Dedicated workflows supply files, index IDs, or completed result receipts.
 * Keep their actions in the catalog and drawer registry, but do not offer raw
 * helper forms in the shared ribbon/menu navigation. */
export function isStandaloneRibbonAction(canonicalKind: string): boolean {
  return !canonicalKind.startsWith('import.')
    && !canonicalKind.startsWith('export.')
    && !canonicalKind.startsWith('embedding.')
    && canonicalKind !== 'resolve.entities';
}
