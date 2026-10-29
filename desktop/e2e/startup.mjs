import { readFile } from 'node:fs/promises';
import { setTimeout as delay } from 'node:timers/promises';

/** Follow the card into Home, failing on real in-card errors without Retry. */
export async function waitForStartup(electron, { timeout = 300_000, diagnosticsPath, pollMs = 200 } = {}) {
  const deadline = Date.now() + timeout;
  const observed = new Set();
  try {
    while (true) {
      const pages = electron.windows().filter(page => !page.isClosed());
      for (const page of pages) {
        if (!page.url().startsWith('file:') || !page.url().endsWith('/startup.html')) continue;
        let state;
        try {
          state = await page.evaluate(() => {
            if (!window.__frisketStartupHarness && window.frisketStartup) {
              const proof = window.__frisketStartupHarness = {};
              proof.unsubscribe = window.frisketStartup.subscribe(state => { proof.state = state; });
            }
            return window.__frisketStartupHarness?.state;
          });
          observed.add(page);
        } catch (error) {
          // A successful card closes as Home opens; navigation can also replace
          // its execution context between collecting windows and reading state.
          if (page.isClosed() || !page.url().endsWith('/startup.html')
            || /Execution context was destroyed|Target page.*closed/i.test(error.message)) continue;
          throw error;
        }
        if (state && ['failed', 'stopped'].includes(state.status)) {
          const saved = diagnosticsPath ? await readFile(diagnosticsPath, 'utf8').catch(() => '') : '';
          const error = new Error(`Desktop startup failed (no retry): ${state.failure?.cause || 'unknown'}\n${JSON.stringify(state, null, 2)}${saved ? `\nSaved startup diagnostics:\n${saved}` : ''}`);
          error.startupPage = page;
          throw error;
        }
      }
      const home = pages.find(page => page.url().startsWith('frisket://app/'));
      if (home) return home;
      if (Date.now() >= deadline) throw new Error(`Desktop startup did not open Home within ${timeout}ms.`);
      await delay(pollMs);
    }
  } finally {
    await Promise.all([...observed].map(page => page.evaluate(() => {
      window.__frisketStartupHarness?.unsubscribe?.();
      delete window.__frisketStartupHarness;
    }).catch(() => {})));
  }
}

/** Attach before waiting so early Home events survive a separate-window handoff. */
export function observeWindows(electron, observe) {
  const seen = new WeakSet();
  const attach = page => {
    if (seen.has(page)) return;
    seen.add(page);
    observe(page);
  };
  electron.on('window', attach);
  for (const page of electron.windows()) attach(page);
  return () => electron.removeListener('window', attach);
}
