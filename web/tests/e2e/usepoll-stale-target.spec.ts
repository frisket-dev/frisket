// The project job resource owns ONE private run target across successive runs.
// Its overlap guard stops concurrent ticks within a target, but an unresolved
// tick for run A can still resolve AFTER replacement by run B. The stale A
// settlement must not publish A or clear B's target/lane.
//
// WHY THE COPILOT PATH: the action FORM's Run button disables while a run is
// active (`running` → "Queued…"), so you cannot launch an overlapping direct
// run there. The Copilot's per-proposal Run button is gated only by ran.has(key),
// NOT by global run state, so it installs the private run-B target while run
// A's tick is still in flight — the real, reachable window for the race.
//
// Route-interception repro: hold run A's status response, launch run B (second
// proposal) while A is in flight, then release A with a TERMINAL payload. The
// replacement retires A's slot without resetting the fixed 300 ms phase, so B
// may poll before A settles. The assertion states the DESIRED post-fix
// behaviour (B keeps polling), so it is RED before the fix (B never polled →
// times out) and GREEN after.

import { expect, test } from '@playwright/test';
import { v1ActionResultPayload, v1ActionStatusPayload } from './actionStatusFixtures';
import { createProject, importCsv, uniqueName } from './helpers';

const RUN_A = 5001;
const RUN_B = 5002;

function classifyProposal(title: string, sheetId: number) {
  return {
    kind: 'map',
    title,
    spec: {
      action_kind: 'map.classify',
      authoring_contract_version: 1,
      params: {
        sheet_id: sheetId,
        input_columns: ['note'],
        context: 'Classify each note.',
        model: 'gemini/gemini-2.5-flash',
        fields: [
          {
            name: `beat_${title.replace(/\W+/g, '_')}`,
            type: 'category',
            description: 'Best-fit beat',
            labels: ['a', 'b'],
          },
        ],
      },
    },
  };
}

test('a stale in-flight tick for run A must not kill run B\'s polling', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-usepoll-stale'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'note\nalpha\nbeta\n');

  // Copilot offers two independently-runnable proposals from one send.
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'Two options.',
        needs_import: false,
        proposals: [classifyProposal('Run A', Number(sheetId)), classifyProposal('Run B', Number(sheetId))],
      }),
    });
  });

  // Each proposal Run posts here; first launch → run A, second → run B.
  let launches = 0;
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    launches += 1;
    const runId = launches === 1 ? RUN_A : RUN_B;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        v1ActionResultPayload({ projectId: pid, runId, actionKind: 'map.classify', status: 'running' }),
      ),
    });
  });

  // Status poller: A is HELD until released (then terminal); B is a live running
  // run that keeps polling if the shared loop survives.
  let aPolls = 0;
  let bPolls = 0;
  let releaseA: () => void = () => {};
  const aHeld = new Promise<void>((resolve) => {
    releaseA = resolve;
  });
  await page.route(`**/api/projects/${pid}/actions/runs/*/status`, async (route) => {
    const runId = Number(route.request().url().match(/runs\/([^/]+)\/status$/)?.[1]);
    if (runId === RUN_A) {
      aPolls += 1;
      await aHeld;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          v1ActionStatusPayload({
            projectId: pid,
            runId: RUN_A,
            actionKind: 'map.classify',
            actionName: 'Run A',
            status: 'completed',
            total: 2,
            completed: 2,
          }),
        ),
      });
      return;
    }
    if (runId === RUN_B) {
      bPolls += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          v1ActionStatusPayload({
            projectId: pid,
            runId: RUN_B,
            actionKind: 'map.classify',
            actionName: 'Run B',
            status: 'running',
            total: 2,
            completed: 1,
          }),
        ),
      });
      return;
    }
    await route.continue();
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await page.getByTestId('chrome-copilot-toggle').click();
  await page.getByTestId('copilot-input').fill('classify these');
  await page.getByTestId('copilot-send').click();
  await expect(page.getByTestId('copilot-proposal').first()).toContainText('Run A');
  await expect(page.getByTestId('copilot-run')).toHaveCount(2);

  // Launch run A (first proposal). Its first status tick lands ~300ms later and
  // is HELD in flight.
  await page.getByTestId('copilot-run').nth(0).click();
  await expect.poll(() => aPolls, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);

  // Switch the target to run B (second proposal) while A's tick is still in
  // flight. Replacement retires A's slot without resetting the fixed phase,
  // so B is eligible on the next 300 ms tick. Wait for B's launch to fully
  // commit (POST settled → private target B) so the release below
  // deterministically resolves A AFTER the target has moved.
  await page.getByTestId('copilot-run').nth(1).click();
  await expect.poll(() => launches, { timeout: 10_000 }).toBe(2);
  await expect.poll(() => bPolls, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);
  const bPollsBeforeRelease = bPolls;

  // Release A with a TERMINAL payload. A stale, target-unaware resolution would
  // clear the private target here and kill B's polling.
  releaseA();

  // DESIRED: the loop survives the stale A resolution and keeps polling B.
  // Require two more requests so a poll already racing at release cannot pass.
  await expect.poll(() => bPolls, { timeout: 6_000 }).toBeGreaterThanOrEqual(
    bPollsBeforeRelease + 2,
  );
});
