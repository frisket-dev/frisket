// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { resolveWalkthroughTarget, walkthroughTargetCandidates } from '../../src/walkthrough/targets';

afterEach(() => { document.body.innerHTML = ''; });

describe('shared documentation and walkthrough targets', () => {
  it('keeps tour navigation fallback order while exposing both candidates', () => {
    document.body.innerHTML = '<button data-testid="menu-action-map.extract">Menu</button><button data-testid="ribbon-action-map.extract">Ribbon</button>';
    const target = { kind: 'action' as const, id: 'map.extract' };
    expect(walkthroughTargetCandidates(target)).toHaveLength(2);
    expect(resolveWalkthroughTarget(target)?.textContent).toBe('Ribbon');
    document.querySelector<HTMLElement>('[data-testid="ribbon-action-map.extract"]')!.hidden = true;
    expect(resolveWalkthroughTarget(target)?.textContent).toBe('Menu');
  });
  it('keeps endpoint and sheet semantic matching and completed-run readiness', () => {
    document.body.innerHTML = '<div data-tour="local-endpoint-row" data-endpoint-label="Other"></div><div data-tour="local-endpoint-row" data-endpoint-label="Local"></div><div data-testid="workbench-mainView-tab-1"><span class="workbench-mainView-tab-name">Filings</span></div><button data-testid="run-watcher-toggle" data-run-status="complete" data-run-failed-rows="0"></button>';
    expect(resolveWalkthroughTarget({ kind: 'local-endpoint', label: 'Local', part: 'row' })?.dataset.endpointLabel).toBe('Local');
    const target = { kind: 'completed-run' as const, target: { kind: 'sheet' as const, id: 'Filings' } };
    expect(resolveWalkthroughTarget(target)).not.toBeNull();
    document.querySelector<HTMLElement>('[data-testid="run-watcher-toggle"]')!.dataset.runFailedRows = '1';
    expect(resolveWalkthroughTarget(target)).toBeNull();
  });
});
