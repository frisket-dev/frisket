/** Exact markers emitted in Ask markdown. Other reserved fragments stay inert. */
export function markerIndex(href: string, prefix: string): number | null {
  const match = new RegExp(`^#${prefix}([1-9]\\d*)$`).exec(href);
  if (!match) return null;
  const index = Number(match[1]);
  return Number.isSafeInteger(index) ? index : null;
}

export function hasInlineMarker(text: string, prefix: string, valid: (index: number) => boolean): boolean {
  return Array.from(text.matchAll(new RegExp(`\\[[^\\]]+\\]\\(#${prefix}([1-9]\\d*)\\)`, 'g')))
    .some((match) => valid(Number(match[1])));
}

export function actionKind(href: string): string | null {
  return /^#action\/([a-z][a-z0-9_.-]*)$/.exec(href)?.[1] ?? null;
}

export function isReservedReference(href: string): boolean {
  return href.startsWith('#cite-') || href.startsWith('#action-') || href.startsWith('#action/');
}
