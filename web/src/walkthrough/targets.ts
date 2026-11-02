import type { WalkthroughTarget } from './walkthroughs';

/** Ordered selector alternatives shared by tours and documentation capture. */
export function walkthroughTargetCandidates(target: WalkthroughTarget): string[] {
  const testId = (id: string) => `[data-testid=${JSON.stringify(id)}]`;
  switch (target.kind) {
    case 'completed-run': return walkthroughTargetCandidates(target.target);
    case 'action-tab': return [testId(`ribbon-tab-${target.id}`), testId(`menubar-menu-${target.id}`)];
    case 'action': return [testId(`ribbon-action-${target.id}`), testId(`menu-action-${target.id}`)];
    case 'command': return [testId(`ribbon-command-${target.id}`), testId(`menu-command-${target.id}`)];
    case 'discover-tab': return [testId(`discover-tab-${target.id}`), testId(`discover-rail-icon-${target.id}`)];
    case 'sheet': return ['[data-testid^="workbench-mainView-tab-"]'];
    case 'grid-column': return [testId(`grid-column-${target.id}`)];
    case 'view': return [testId(`view-switch-${target.id}`)];
    case 'tour': return [`[data-tour=${JSON.stringify(target.id)}]`];
    case 'local-endpoint': return [`[data-tour=${JSON.stringify(target.part === 'row' ? 'local-endpoint-row' : 'local-endpoint-guidance')}]`];
    case 'field':
    case 'element': return [testId(target.id)];
  }
}

/** Match additional semantic attributes after finding selector candidates. */
export function matchesWalkthroughTarget(element: HTMLElement, target: WalkthroughTarget): boolean {
  if (target.kind === 'completed-run') return matchesWalkthroughTarget(element, target.target);
  if (target.kind === 'sheet') return element.querySelector<HTMLElement>('.workbench-mainView-tab-name')?.textContent?.trim() === target.id;
  if (target.kind === 'local-endpoint') return element.dataset.endpointLabel === target.label;
  return true;
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

/** Resolve semantic walkthrough targets against either action-navigation density. */
export function resolveWalkthroughTarget(target: WalkthroughTarget): HTMLElement | null {
  if (target.kind === 'completed-run') {
    const status = resolveWalkthroughTarget({ kind: 'element', id: 'run-watcher-toggle' });
    const failedRows = Number(status?.dataset.runFailedRows);
    if (!status || status.dataset.runStatus !== 'complete' || !Number.isFinite(failedRows) || failedRows !== 0) return null;
  }
  for (const selector of walkthroughTargetCandidates(target)) {
    const found = Array.from(document.querySelectorAll<HTMLElement>(selector))
      .find((element) => isAvailable(element) && matchesWalkthroughTarget(element, target));
    if (found) return found;
  }
  return null;
}
