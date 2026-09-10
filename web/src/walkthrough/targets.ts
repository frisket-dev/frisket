import type { WalkthroughTarget } from './walkthroughs';

function elementsWithTestId(testId: string): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-testid]')).filter(
    (element) => element.dataset.testid === testId,
  );
}

function elementsWithTourAnchor(anchor: string): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-tour]')).filter(
    (element) => element.dataset.tour === anchor,
  );
}

function localEndpointPart(
  label: string,
  part: 'row' | 'guidance',
): HTMLElement | null {
  const anchor = part === 'row' ? 'local-endpoint-row' : 'local-endpoint-guidance';
  return elementsWithTourAnchor(anchor).find((element) => (
    element.dataset.endpointLabel === label && isAvailable(element)
  )) ?? null;
}

function isAvailable(element: HTMLElement): boolean {
  let current: HTMLElement | null = element;
  while (current) {
    if (current.hidden || current.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    current = current.parentElement;
  }
  return true;
}

function firstAvailable(testIds: readonly string[]): HTMLElement | null {
  for (const testId of testIds) {
    const match = elementsWithTestId(testId).find(isAvailable);
    if (match) return match;
  }
  return null;
}

function latestRunCompletedSuccessfully(): boolean {
  const status = firstAvailable(['run-watcher-toggle']);
  if (!status || status.dataset.runStatus !== 'complete') return false;
  const failedRows = Number(status.dataset.runFailedRows);
  return Number.isFinite(failedRows) && failedRows === 0;
}

function sheetTabWithName(name: string): HTMLElement | null {
  return Array.from(
    document.querySelectorAll<HTMLElement>('[data-testid^="workbench-mainView-tab-"]'),
  ).find((element) => (
    isAvailable(element)
    && element.querySelector<HTMLElement>('.workbench-mainView-tab-name')?.textContent?.trim() === name
  )) ?? null;
}

/** Resolve semantic walkthrough targets against either action-navigation density. */
export function resolveWalkthroughTarget(target: WalkthroughTarget): HTMLElement | null {
  switch (target.kind) {
    case 'completed-run':
      return latestRunCompletedSuccessfully()
        ? resolveWalkthroughTarget(target.target)
        : null;
    case 'action-tab':
      return firstAvailable([
        `ribbon-tab-${target.id}`,
        `menubar-menu-${target.id}`,
      ]);
    case 'action':
      return firstAvailable([
        `ribbon-action-${target.id}`,
        `menu-action-${target.id}`,
      ]);
    case 'command':
      return firstAvailable([
        `ribbon-command-${target.id}`,
        `menu-command-${target.id}`,
      ]);
    case 'discover-tab':
      return firstAvailable([
        `discover-tab-${target.id}`,
        `discover-rail-icon-${target.id}`,
      ]);
    case 'sheet':
      return sheetTabWithName(target.id);
    case 'grid-column':
      return firstAvailable([`grid-column-${target.id}`]);
    case 'view':
      return firstAvailable([`view-switch-${target.id}`]);
    case 'tour':
      return elementsWithTourAnchor(target.id).find(isAvailable) ?? null;
    case 'local-endpoint':
      return localEndpointPart(target.label, target.part);
    case 'field':
    case 'element':
      return firstAvailable([target.id]);
  }
}
